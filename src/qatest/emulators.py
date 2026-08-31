"""Cloud emulators for a test run: podman lifecycle, selected from the graph.

Which emulators start is DERIVED, not configured. The Dependency nodes code analysis
already wrote say which clouds a project touches, so a project using only boto3 starts
one emulator and a project touching no cloud starts none. Configuration cannot drift
from the code because it is the same analysis.

Podman drives the images directly rather than floci-cli: the CLI is Docker-oriented,
and this code has to own the lifecycle anyway so it can start exactly what the graph
implies and stop it when the run ends.

Verified on this machine: floci 1.7.0 native starts under ROOTLESS podman in ~40ms
with no Docker socket and no privileged mode, and the real AWS CLI drives S3,
DynamoDB, SQS and Secrets Manager against it.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from src.qatest.types import EmulatorRecord

log = logging.getLogger(__name__)

_TIMEOUT_S = 30


def _ready_timeout() -> int:
    """How long to wait for an emulator to answer. Settings-backed so a slow machine
    can raise it without a code change; the default suits a warm image."""
    try:
        from src.config_settings import get_settings
        return int(get_settings().qatest_emulator_timeout_s)
    except Exception:  # noqa: BLE001 — a probe must work without app settings loaded
        return 60


class Cloud:
    """One emulator: its image, port, and how a project's dependencies imply it."""

    def __init__(self, name: str, image: str, port: int, markers: tuple[str, ...],
                 env: dict[str, str]):
        self.name = name
        self.image = image
        self.port = port
        self.markers = markers
        self.env_template = env

    def env(self) -> dict[str, str]:
        return {k: v.format(port=self.port) for k, v in self.env_template.items()}


# Ports are Floci's own, so a developer already running Floci by hand sees the same
# endpoints. The env vars are what point the application under test at the emulator
# with NO change to its code — verified for AWS against this repo's pinned boto3,
# where AWS_ENDPOINT_URL alone redirects every service client at once.
CLOUDS: tuple[Cloud, ...] = (
    Cloud("aws", "docker.io/floci/floci:latest", 4566,
          ("boto3", "botocore", "aws-sdk", "@aws-sdk/", "aws-cdk", "awscli"),
          {"AWS_ENDPOINT_URL": "http://localhost:{port}",
           "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
           "AWS_DEFAULT_REGION": "us-east-1"}),
    Cloud("azure", "docker.io/floci/floci-az:latest", 4577,
          ("azure-", "@azure/", "azure.storage", "azure-identity"),
          {"AZURE_STORAGE_CONNECTION_STRING":
           "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
           "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
           "BlobEndpoint=http://localhost:{port}/devstoreaccount1;",
           "AZURE_ENDPOINT_URL": "http://localhost:{port}"}),
    Cloud("gcp", "docker.io/floci/floci-gcp:latest", 4588,
          ("google-cloud-", "@google-cloud/", "googleapis"),
          {"STORAGE_EMULATOR_HOST": "http://localhost:{port}",
           "PUBSUB_EMULATOR_HOST": "localhost:{port}",
           "FIRESTORE_EMULATOR_HOST": "localhost:{port}",
           "GOOGLE_CLOUD_PROJECT": "aura-local"}),
    Cloud("oci", "docker.io/floci/floci-oci:latest", 4599,
          # "oci" matched EXACTLY (it is the real PyPI package name) plus "oci-" as a
          # prefix. A bare 3-letter prefix would drag in anything containing "oci",
          # e.g. a package called "social".
          ("oci", "oci-", "oracle-cloud"),
          {"OCI_ENDPOINT_URL": "http://localhost:{port}",
           "OCI_CLI_ENDPOINT": "http://localhost:{port}"}),
)

_BY_NAME = {c.name: c for c in CLOUDS}


def clouds_for(dependencies: list[dict]) -> list[Cloud]:
    """Which emulators this project needs, from its Dependency nodes.

    Matching is on a normalised prefix so `azure-storage-blob` and `@azure/identity`
    both resolve, while a package that merely mentions a cloud in passing does not.
    `oci` is matched exactly — plenty of package names contain those three letters.
    """
    names = {str(d.get("name") or "").strip().lower() for d in dependencies or []}
    names.discard("")

    needed: list[Cloud] = []
    for cloud in CLOUDS:
        for pkg in names:
            if any(pkg == m or pkg.startswith(m) for m in cloud.markers
                   if len(m) > 3 or pkg == m):
                needed.append(cloud)
                break
    return needed


def podman_available() -> bool:
    return shutil.which("podman") is not None


def _run(args: list[str], timeout: int = _TIMEOUT_S) -> tuple[int, str]:
    try:
        p = subprocess.run(["podman", *args], capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"podman {' '.join(args)} timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def image_digest(image: str) -> str:
    """The pinned digest of a local image.

    Recorded in the report because a result is only evidence if the thing that
    produced it is identifiable, and `latest` moves.
    """
    code, out = _run(["image", "inspect", image, "--format", "{{.Digest}}"])
    if code == 0 and out.strip().startswith("sha256:"):
        return out.strip()
    return ""


def _ready(port: int, timeout: int | None = None) -> bool:
    """Wait until the emulator answers HTTP on its port.

    Any status code counts. An emulator that replies 404 to `/` is up; requiring 200
    would wait forever on a service with no root route.
    """
    timeout = timeout if timeout is not None else _ready_timeout()
    deadline = time.monotonic() + timeout
    url = f"http://localhost:{port}/"
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.5)
    return False


class EmulatorSet:
    """Starts the emulators a run needs and guarantees they are stopped.

    Use as a context manager: containers are removed on the way out even when the
    run raises, because a leaked emulator holds a port and the next run then fails
    for a reason that looks nothing like the cause.
    """

    def __init__(self, clouds: list[Cloud], run_id: str):
        self.clouds = clouds
        self.run_id = run_id
        self.records: list[EmulatorRecord] = []

    @property
    def env(self) -> dict[str, str]:
        """Environment that points an application at whatever actually started."""
        out: dict[str, str] = {}
        started = {r.cloud for r in self.records if r.started}
        for cloud in self.clouds:
            if cloud.name in started:
                out.update(cloud.env())
        return out

    def __enter__(self) -> "EmulatorSet":
        for cloud in self.clouds:
            self.records.append(self._start(cloud))
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _start(self, cloud: Cloud) -> EmulatorRecord:
        name = f"aura-qa-{cloud.name}-{self.run_id}"
        rec = EmulatorRecord(cloud=cloud.name, image=cloud.image,
                             digest=image_digest(cloud.image), port=cloud.port,
                             container=name)

        if not podman_available():
            rec.error = "podman not found on PATH"
            return rec

        # A container left behind by an interrupted run holds the name and the port.
        _run(["rm", "-f", name])

        code, out = _run(["run", "-d", "--name", name,
                          "-p", f"{cloud.port}:{cloud.port}", cloud.image], timeout=120)
        if code != 0:
            rec.error = out.strip()[-400:]
            return rec

        if not _ready(cloud.port):
            logs = _run(["logs", "--tail", "20", name])[1]
            rec.error = (f"did not answer on :{cloud.port} within "
                         f"{_ready_timeout()}s. {logs[-300:]}")
            _run(["rm", "-f", name])
            return rec

        rec.started = True
        log.info("qatest: %s emulator ready on :%s (%s)", cloud.name, cloud.port,
                 rec.digest[:19] or "no digest")
        return rec

    def stop(self) -> None:
        for rec in self.records:
            if rec.container:
                _run(["rm", "-f", rec.container])
        log.info("qatest: emulators stopped")


def probe(cloud_name: str) -> dict:
    """Start one emulator, confirm it answers, stop it. Used by the CLI and tests."""
    cloud = _BY_NAME.get(cloud_name)
    if not cloud:
        return {"ok": False, "message": f"unknown cloud {cloud_name!r}"}
    with EmulatorSet([cloud], "probe") as es:
        rec = es.records[0]
        return {"ok": rec.started, "message": rec.error or "ready",
                "digest": rec.digest, "port": rec.port, "env": es.env}
