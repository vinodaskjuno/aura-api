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

#: Container names Aura considers its own. TWO prefixes, because an emulator now has two
#: possible lifetimes: `aura-qa-<cloud>-<runId>` lives and dies with one test run, while
#: `aura-dev-<cloud>-<projectId>` is started by a developer from DevMate and lives until
#: they stop it. Every "is this ours" decision must accept both — the same tuple is
#: enforced independently on the server (queue.MANAGED_PREFIXES), because one side
#: trusting the other is how a laptop ends up reading out arbitrary container logs.
MANAGED_PREFIXES = ("aura-qa-", "aura-dev-")

#: Project-scoped containers, started from DevMate and stopped only on request.
DEV_PREFIX = "aura-dev-"

#: The named network Floci's container-backed services need. Rootless podman's default
#: bridge gives containers no reachable IPs for each other, so Lambda's Runtime API
#: callback never arrives without this.
CONTAINER_NETWORK = "aura-floci"


def dev_container(cloud: str, project_id: str) -> str:
    """The name of a project-scoped emulator. Stable, so Start is idempotent and Stop
    can find what Start created without recording anything."""
    return f"{DEV_PREFIX}{cloud}-{project_id}"


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


def podman_path() -> str | None:
    """Where podman actually is, searching install locations as well as PATH.

    `shutil.which("podman")` answers a narrower question and got it wrong: the .pkg
    installs to /opt/podman/bin, which is not on the PATH of a process launched from
    anywhere but a login shell — so the runner refused to start on machines where
    aura-infra's own scripts (which export that directory) work fine.
    """
    from src.qatest import toolpath
    return toolpath.which("podman")


def podman_available() -> bool:
    return podman_path() is not None


def podman_ready() -> tuple[bool, str]:
    """Is podman actually usable — not merely installed. (ok, reason).

    On macOS and Windows podman is a client for a VM, so `which` succeeds while every
    command fails. That is not hypothetical: preflight passed, the agent claimed a run
    and marked it running, and only then did each emulator fail, which is the worst
    possible moment to find out.
    """
    binary = podman_path()
    if not binary:
        return False, "podman is not installed, or is not in any known location"
    code, out = _run(["info", "--format", "{{.Host.RemoteSocket.Exists}}"], timeout=20)
    if code == 0:
        return True, ""
    lowered = out.lower()
    if "machine" in lowered or "connection" in lowered or "socket" in lowered:
        return False, ("podman is installed but not running — on macOS and Windows it "
                       "needs its virtual machine started: `podman machine start`")
    return False, f"podman is installed but not usable: {out.strip()[-200:]}"


def _run(args: list[str], timeout: int = _TIMEOUT_S) -> tuple[int, str]:
    from src.qatest import toolpath

    # The ABSOLUTE path and an augmented PATH, not one or the other: podman execs its
    # own helpers (gvproxy, vfkit) out of the same directory, so an absolute podman
    # with a bare PATH still cannot start a machine.
    binary = toolpath.which("podman") or "podman"
    try:
        p = subprocess.run([binary, *args], capture_output=True, text=True,
                           timeout=timeout, env=toolpath.env())
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"podman {' '.join(args)} timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def _container_on_port(port: int) -> dict:
    """The container publishing `port`, as {name, image}. Empty when none is found.

    `--filter publish=` is NOT used: podman rejects it as an invalid filter on the
    versions this has to run against. Parsing the Ports column is uglier and works.
    """
    code, out = _run(["ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Ports}}"])
    if code != 0:
        return {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, image, ports = parts[0], parts[1], parts[2]
        # Matches "0.0.0.0:4566->4566/tcp" and "[::]:4566->4566/tcp" without also
        # matching a container that merely EXPOSES 4566 without publishing it.
        if f":{port}->" in ports:
            return {"name": name.strip(), "image": image.strip()}
    return {}


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

    def __init__(self, clouds: list[Cloud], run_id: str, on_event=None):
        self.clouds = clouds
        self.run_id = run_id
        self.records: list[EmulatorRecord] = []
        # So the UI can watch containers come up and go away. Without an event on the
        # way OUT, a panel can only ever learn that an emulator started.
        self.on_event = on_event

    def _emit(self, **data) -> None:
        if not self.on_event:
            return
        try:
            self.on_event({"type": "emulator", **data})
        except Exception:                                     # noqa: BLE001
            pass

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
            self._emit(cloud=cloud.name, image=cloud.image, port=cloud.port,
                       container=f"aura-qa-{cloud.name}-{self.run_id}",
                       starting=True, started=False,
                       message=f"bringing up the {cloud.name} emulator on :{cloud.port}")
            rec = self._start(cloud)
            self.records.append(rec)
            # "adopted" and "started" are different facts and the reader acts on them
            # differently — one of them means this run will clean up afterwards and the
            # other means it will not.
            # Said once, on the first cloud, and only when it is actually a limitation:
            # a run whose project needs no Lambda should not be told about sockets.
            reason = socket_unavailable_reason()
            if reason and rec.started:
                self._emit(cloud=rec.cloud, container=rec.container, port=rec.port,
                           started=True,
                           message=f"container-backed services unavailable — {reason}. "
                                   f"Lambda cases will be recorded unemulated.")

            if rec.adopted:
                message = (f"{rec.cloud} emulator already running on :{rec.port} — "
                           f"adopted, and will be left alone")
            elif rec.started:
                message = f"{rec.cloud} emulator ready on :{rec.port}"
            else:
                message = f"{rec.cloud} emulator failed: {rec.error[:160]}"
            self._emit(**rec.as_dict(), message=message)
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _start(self, cloud: Cloud) -> EmulatorRecord:
        name = f"aura-qa-{cloud.name}-{self.run_id}"
        rec = EmulatorRecord(cloud=cloud.name, image=cloud.image,
                             digest=image_digest(cloud.image), port=cloud.port,
                             container=name)

        # `podman_ready`, not `podman_available`: being installed is not being usable.
        # A stopped machine used to pass every check and then fail here with a raw exec
        # error, once per cloud, after the run had already been claimed.
        ready, why = podman_ready()
        if not ready:
            rec.error = why
            return rec

        # Something is ALREADY serving this port: adopt it instead of colliding.
        #
        # Floci's ports are fixed and are Floci's own, so a developer who ran
        # `floci start`, or who started one from DevMate, is holding exactly the port
        # this run wants. Until this existed the run simply died — podman answers
        # "proxy already running" — which made running your own emulator and running
        # tests mutually exclusive.
        #
        # Probed BEFORE `podman run`, with a short timeout: the full readiness wait
        # belongs after a start we actually performed, not in front of every run.
        if _ready(cloud.port, timeout=2):
            found = _container_on_port(cloud.port)
            rec.container = found.get("name", "")
            # The image that is REALLY there, not the one this run would have used.
            # They differ whenever the operator pinned a version, and reporting ours
            # would make the report describe a container that never existed.
            rec.image = found.get("image", "") or cloud.image
            rec.digest = image_digest(rec.image)
            rec.adopted = True
            rec.started = True
            log.info("qatest: adopted the %s emulator already on :%s (%s)",
                     cloud.name, cloud.port, rec.container or "unknown container")
            return rec

        # Through the shared helper, so a run and a DevMate start bring a container up
        # exactly the same way — the container-runtime socket included. Two code paths
        # for "start Floci" is two paths for them to drift.
        ok, why = start_container(name, cloud)
        if not ok:
            rec.error = why
            return rec

        rec.started = True
        log.info("qatest: %s emulator ready on :%s (%s)", cloud.name, cloud.port,
                 rec.digest[:19] or "no digest")
        return rec

    def stop(self) -> None:
        for rec in self.records:
            # Never remove what this run did not start. An adopted emulator belongs to
            # whoever started it — `floci-cli` or DevMate — and they stop it when they
            # choose. Tearing it down here would make "run the tests" a destructive act
            # on someone's working environment.
            if rec.adopted:
                self._emit(cloud=rec.cloud, container=rec.container, port=rec.port,
                           started=True, stopped=False, adopted=True,
                           message=f"{rec.cloud} emulator left running "
                                   f"(started outside this run)")
                continue
            if rec.container:
                _run(["rm", "-f", rec.container])
                self._emit(cloud=rec.cloud, container=rec.container, port=rec.port,
                           image=rec.image, digest=rec.digest,
                           started=False, stopped=True,
                           message=f"{rec.cloud} emulator stopped")
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


# ── Reporting the machine's own state ──────────────────────────────────────────
#
# Only the runner can see podman; the API runs on Fargate. These are called BY the
# agent and their output is shipped to the server, which is the only way "show Floci
# running on your machine" can be answered at all.

def list_containers(include_unmanaged: bool = False) -> list[dict]:
    """Aura's Floci containers as podman reports them, right now.

    Defaults to Aura's own containers ONLY. A developer's machine runs their
    employer's containers, and shipping every name on it to a shared server is a data
    leak dressed as a feature — `include_unmanaged` exists for the agent's explicit
    --report-all-containers flag and nothing else.
    """
    if not podman_available():
        return []
    code, out = _run(["ps", "--format", "json"], timeout=20)
    if code != 0 or not out.strip():
        return []
    try:
        raw = json.loads(out)
    except (ValueError, TypeError) as exc:
        log.debug("qatest: could not parse podman ps: %s", exc)
        return []

    containers = []
    for item in raw if isinstance(raw, list) else []:
        names = item.get("Names") or item.get("names") or []
        name = (names[0] if isinstance(names, list) and names else str(names or ""))
        if not include_unmanaged and not name.startswith(MANAGED_PREFIXES):
            continue
        ports = item.get("Ports") or []
        containers.append({
            "id": str(item.get("Id") or item.get("ID") or "")[:12],
            "name": name,
            "image": str(item.get("Image") or ""),
            "status": str(item.get("Status") or item.get("State") or ""),
            "ports": _format_ports(ports),
            "createdAt": str(item.get("CreatedAt") or ""),
            "cloud": _cloud_of(name),
        })
    return containers


def _cloud_of(container_name: str) -> str:
    """The cloud a container serves, from `aura-<qa|dev>-<cloud>-<id>`.

    Both prefixes put the cloud in the same position, so one index covers them — but a
    name that does not match is reported as unknown rather than guessed at, which is why
    the membership test against _BY_NAME stays.
    """
    parts = container_name.split("-")
    return parts[2] if len(parts) > 3 and parts[2] in _BY_NAME else ""


def _format_ports(ports) -> str:
    if isinstance(ports, str):
        return ports
    out = []
    for p in ports if isinstance(ports, list) else []:
        if isinstance(p, dict):
            host = p.get("host_port") or p.get("hostPort") or ""
            cont = p.get("container_port") or p.get("containerPort") or ""
            if host or cont:
                out.append(f"{host}->{cont}")
        else:
            out.append(str(p))
    return ", ".join(out)[:120]


def _runtime_socket() -> str:
    """The container runtime socket, or "" when there is none to give.

    Read from podman rather than assembled from a uid: it is /run/user/501/... on this
    Mac and /run/user/1000/... on a typical Linux box, and a hardcoded path fails on
    whichever one you did not test.
    """
    code, out = _run(["info", "--format", "{{.Host.RemoteSocket.Path}}"])
    path = out.strip() if code == 0 else ""
    return path.replace("unix://", "") if path.startswith("/") or path.startswith(
        "unix://") else ""


#: Why container-backed services are off, when they are. Read by EmulatorSet so the
#: reason reaches the run rather than only the runner's log — a one-slot list because
#: `_socket_args` is a module function called from a method.
_socket_unavailable: list[str] = [""]


def socket_unavailable_reason() -> str:
    """Why Lambda and friends cannot run, or "" when they can."""
    return _socket_unavailable[0]


def _socket_args(cloud: "Cloud") -> list[str]:
    """Flags that let Floci start containers of its own, or [] when not enabled.

    Floci's container-backed services — Lambda above all — need a Docker-compatible
    socket, a NAMED network (the rootless default bridge assigns no reachable
    inter-container IPs), and a stable hostname for the Lambda Runtime API callback.

    Returns [] rather than raising when unavailable: a run without Lambda is a run with
    one `unemulated` case, which is a far better outcome than a run that will not start.
    """
    try:
        from src.config_settings import get_settings
        if not get_settings().qatest_container_backed_services:
            return []
    except Exception:                                         # noqa: BLE001
        return []

    socket = _runtime_socket()
    if not socket:
        # Returned [] in silence before. A Lambda case then failed as `unemulated` with
        # no cause anywhere in the run, which is the same shape as "we did not try".
        log.warning("qatest: container-backed services are enabled but podman reports "
                    "no socket — Lambda and friends will be unavailable")
        _socket_unavailable[0] = (
            "podman reports no container runtime socket, so Floci cannot start Lambda "
            "containers")
        return []
    _socket_unavailable[0] = ""

    # Idempotent: `network create` fails when it already exists, and that is fine.
    _run(["network", "create", CONTAINER_NETWORK])
    return [
        "--network", CONTAINER_NETWORK,
        # The function reaches Floci by the hostname FLOCI_HOSTNAME advertises, but that
        # variable only tells Floci what to SAY — it creates no DNS. Aura's containers
        # are named aura-qa-aws-<runId>, so without this alias the function resolves
        # nothing and fails with EndpointConnectionError from inside the handler, which
        # looks like the function's own bug rather than a networking gap.
        "--network-alias", "floci",
        # Required, and not a precaution. Measured on rootless podman 5.x / macOS:
        # default caps and `--user root` both fail with
        # `java.net.BindException: Permission denied` when Floci binds its Lambda
        # Runtime API; only --privileged succeeds.
        #
        # This is a LARGE grant on a developer's machine — combined with the socket
        # below, code inside Floci can drive the host's container engine. It is why
        # `qatest_container_backed_services` is opt-in and off by default, and why the
        # demo must never depend on it.
        "--privileged",
        # Lowercase :z — shared relabel. :Z is private and breaks the mount for a
        # second container.
        "-v", f"{socket}:/var/run/docker.sock:z",
        # Mounting the socket is not enough: without this Floci's Docker client falls
        # back to `unix://localhost:2375` and tries to BIND it. The log even says
        # "Creating DockerClient for host: unix:///var/run/docker.sock" and then ignores
        # it, so the mount looks correct while nothing uses it.
        "-e", "DOCKER_HOST=unix:///var/run/docker.sock",
        "-e", f"FLOCI_SERVICES_LAMBDA_DOCKER_NETWORK={CONTAINER_NETWORK}",
        "-e", "FLOCI_HOSTNAME=floci",
    ]


def start_container(name: str, cloud: "Cloud") -> tuple[bool, str]:
    """Bring up one Floci container under an explicit name. (ok, reason).

    Shared by the run path and the DevMate path so there is ONE definition of how a
    Floci container is started — the container-runtime socket included. Two ways to
    start a container is two ways for them to drift.
    """
    if not name.startswith(MANAGED_PREFIXES):
        return False, "refusing to start a container outside Aura's own namespace"
    ready, why = podman_ready()
    if not ready:
        return False, why

    _run(["rm", "-f", name])
    code, out = _run(["run", "-d", "--name", name,
                      "-p", f"{cloud.port}:{cloud.port}",
                      *_socket_args(cloud), cloud.image], timeout=120)
    if code != 0:
        return False, out.strip()[-400:] or f"podman run exited {code}"
    if not _ready(cloud.port):
        logs = _run(["logs", "--tail", "20", name])[1]
        _run(["rm", "-f", name])
        return False, (f"did not answer on :{cloud.port} within {_ready_timeout()}s. "
                       f"{logs[-300:]}")
    return True, ""


def remove_container(name: str) -> tuple[bool, str]:
    """Remove one of Aura's own containers. (ok, reason).

    Prefix-checked like every other operation that names a container: this one DELETES,
    so the consequence of accepting an arbitrary name is worse than for reading logs.
    """
    if not name.startswith(MANAGED_PREFIXES):
        return False, "refusing to stop a container Aura did not start"
    if not podman_available():
        return False, "podman not found on PATH"
    code, out = _run(["rm", "-f", name], timeout=60)
    if code != 0:
        return False, out.strip()[-300:] or f"podman rm exited {code}"
    return True, ""


def container_logs(name: str, tail: int = 200) -> tuple[bool, str]:
    """`podman logs --tail N` for one of Aura's own containers.

    Refuses anything outside MANAGED_PREFIXES, on the RUNNER side as well as the
    server's.
    The check is cheap and the failure mode is severe: without it, a bug or a
    compromised server could read arbitrary container output off a developer's laptop.
    """
    if not name.startswith(MANAGED_PREFIXES):
        return False, "refusing to read logs for a container Aura did not start"
    if not podman_available():
        return False, "podman not found on PATH"
    tail = max(1, min(int(tail or 200), 500))
    code, out = _run(["logs", "--tail", str(tail), name], timeout=30)
    if code != 0:
        return False, out.strip()[-400:] or f"podman logs exited {code}"
    return True, out
