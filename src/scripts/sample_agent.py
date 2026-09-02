"""Send a realistic multi-step agent trace, to prove the AI Observability path works.

    # Against a local Opik directly (no Aura, no key needed — OSS Opik has no auth):
    python -m src.scripts.sample_agent --target opik --url http://localhost:5173/api/

    # Against Aura, the way a customer's agent would (needs a gw- key):
    python -m src.scripts.sample_agent --target aura \
        --url https://<aura-host> --api-key gw-xxxxx --project checkout-agent

    # Same agent shape, but over raw OTLP instead of the Opik SDK:
    python -m src.scripts.sample_agent --target otlp \
        --url https://<aura-host> --api-key gw-xxxxx

Why a THREE-level trace and not one span: the whole point of the traces view is the
tree. A flat single span proves the transport works and nothing else — it would not
catch a broken parent link, a mis-mapped span kind, or usage that is attached to the
wrong level. This emits agent -> (retrieve -> llm -> tool), with token counts and a
deliberate error on the last run so the error path is exercised too.

No real LLM is called. Everything is synthetic, so this is safe to run repeatedly and
costs nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

QUESTIONS = [
    "why did checkout latency spike this morning?",
    "summarise the payment service dependencies",
    "which endpoints regressed after release 4.2?",
    "is the cart service leaking connections?",
]

ANSWERS = [
    "The p95 rose because the payment service pool saturated at 08:14 UTC.",
    "payment-svc depends on cart-svc, ledger-svc and the Stripe gateway.",
    "Two endpoints regressed: POST /cart/items and GET /cart.",
    "Yes — connection count grows monotonically after each deploy.",
]

MODEL = "claude-sonnet-4-5"


# ── Opik SDK path ─────────────────────────────────────────────────────────────

def run_via_sdk(url: str, api_key: str, project: str, runs: int) -> int:
    """Instrument with @track, exactly as the onboarding snippet tells a user to.

    An earlier version of this script drove the low-level client — client.trace(),
    trace.span(), .end() — so it could set usage and model explicitly. That LOST DATA:
    the SDK batches writes, and ending a span milliseconds after creating it races the
    flush. It warns about this ("Calling Span.end() shortly after creation with
    batching enabled may cause data loss") and it was right — two of four traces
    arrived with no name, no usage and no cost.

    @track avoids the race entirely because the SDK owns the span lifecycle, and
    opik_context.update_current_span attaches usage/model/cost to the span the
    decorator is already managing. It is also the API customers will actually write.
    """
    os.environ["OPIK_URL_OVERRIDE"] = url
    os.environ["OPIK_WORKSPACE"] = "default"
    os.environ["OPIK_PROJECT_NAME"] = project
    if api_key:
        os.environ["OPIK_API_KEY"] = api_key

    try:
        import opik
        from opik import opik_context, track
    except ImportError:
        print("ERROR: the opik SDK is not installed.  pip install opik", file=sys.stderr)
        return 1

    @track(project_name=project, name="retrieve_context")
    def retrieve_context(question: str) -> list[str]:
        time.sleep(0.05)
        return [f"doc-{n}" for n in range(3)]

    @track(project_name=project, name="chat_completion", type="llm")
    def chat_completion(question: str, docs: list[str], fail: bool) -> str:
        prompt_tokens = random.randint(900, 1600)
        completion_tokens = random.randint(120, 480)
        time.sleep(0.12)

        if fail:
            # Attach the failure to the span, then raise: @track records the exception
            # on the span AND propagates it, which is what a real outage looks like.
            opik_context.update_current_span(
                model=MODEL, provider="anthropic",
                usage={"prompt_tokens": prompt_tokens, "completion_tokens": 0,
                       "total_tokens": prompt_tokens})
            raise RuntimeError("429 from provider (intentional, for the error path)")

        opik_context.update_current_span(
            model=MODEL, provider="anthropic",
            usage={"prompt_tokens": prompt_tokens,
                   "completion_tokens": completion_tokens,
                   "total_tokens": prompt_tokens + completion_tokens},
        )
        return ANSWERS[hash(question) % len(ANSWERS)]

    @track(project_name=project, name="post_to_slack", type="tool")
    def post_to_slack(answer: str) -> dict:
        time.sleep(0.03)
        return {"channel": "#sre", "delivered": True}

    @track(project_name=project, name="support-agent")
    def support_agent(question: str, fail: bool, thread: str) -> str:
        # thread_id groups every run into one conversation in the Threads view.
        opik_context.update_current_trace(
            thread_id=thread, tags=["sample", "support-agent"],
            metadata={"source": "sample_agent.py"})
        docs = retrieve_context(question)
        answer = chat_completion(question, docs, fail)
        post_to_slack(answer)
        return answer

    thread = f"sample-session-{int(time.time())}"
    ok = 0
    for i in range(runs):
        question = QUESTIONS[i % len(QUESTIONS)]
        # The last run fails on purpose: a provider error is exactly what someone opens
        # the traces view looking for, so the sample must produce one.
        fail = i == runs - 1 and runs > 1
        try:
            support_agent(question, fail, thread)
            ok += 1
            print(f"  run {i + 1}/{runs}: ok")
        except RuntimeError as exc:
            print(f"  run {i + 1}/{runs}: ERROR (intentional) — {exc}")

    opik.flush_tracker()
    print(f"\nflushed {runs} trace(s) to project '{project}' "
          f"({ok} ok, {runs - ok} intentional failure(s))")
    return 0


# ── Raw OTLP path ─────────────────────────────────────────────────────────────

def run_via_otlp(url: str, api_key: str, project: str, runs: int) -> int:
    """Post OTLP/JSON to Aura's own receiver.

    This is the path that needs no Opik dependency at all, and it uses the GenAI
    semantic conventions that src/aiobs/ingest.py normalises. Sent as JSON rather than
    protobuf so the payload is readable when something goes wrong.
    """
    endpoint = f"{url.rstrip('/')}/otlp/v1/traces"
    sent = 0

    for i in range(runs):
        question = QUESTIONS[i % len(QUESTIONS)]
        answer = ANSWERS[i % len(ANSWERS)]
        now_ns = int(time.time() * 1e9)
        trace_id = os.urandom(16).hex()          # 128-bit, as every OTel SDK emits
        root_id = os.urandom(8).hex()
        llm_id = os.urandom(8).hex()
        prompt_tokens = random.randint(900, 1600)
        completion_tokens = random.randint(120, 480)

        def span(span_id: str, name: str, parent: str, start_off: int, end_off: int,
                 attrs: list[dict]) -> dict:
            body = {
                "traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,
                "startTimeUnixNano": str(now_ns - start_off),
                "endTimeUnixNano": str(now_ns - end_off),
                "attributes": attrs, "status": {"code": 1},
            }
            if parent:
                body["parentSpanId"] = parent
            return body

        def kv(key: str, value) -> dict:
            if isinstance(value, bool):
                return {"key": key, "value": {"boolValue": value}}
            if isinstance(value, int):
                return {"key": key, "value": {"intValue": str(value)}}
            return {"key": key, "value": {"stringValue": str(value)}}

        payload = {"resourceSpans": [{
            "resource": {"attributes": [
                # service.name becomes the PROJECT. This is the whole zero-config
                # story: a client appears the moment it starts exporting.
                kv("service.name", project),
                kv("deployment.environment", "sample"),
            ]},
            "scopeSpans": [{
                "scope": {"name": "aura.sample_agent"},
                "spans": [
                    span(root_id, "support-agent", "", 2_000_000_000, 0, [
                        kv("session.id", f"sample-session-{i}"),
                        kv("input.value", question),
                        kv("output.value", answer),
                    ]),
                    span(llm_id, "chat_completion", root_id,
                         1_800_000_000, 400_000_000, [
                             kv("gen_ai.system", "anthropic"),
                             kv("gen_ai.request.model", MODEL),
                             kv("gen_ai.usage.input_tokens", prompt_tokens),
                             kv("gen_ai.usage.output_tokens", completion_tokens),
                             kv("input.value", question),
                             kv("output.value", answer),
                         ]),
                ],
            }],
        }]}

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as exc:
            print(f"  run {i + 1}: HTTP {exc.code} {exc.read()[:200]!r}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  run {i + 1}: {exc}", file=sys.stderr)
            continue

        # The receiver ALWAYS returns 200, even on auth failure, so that a bad key can
        # never make a customer's exporter retry-loop. partialSuccess is the only place
        # a problem shows up.
        parsed = json.loads(body or "{}")
        partial = parsed.get("partialSuccess") or {}
        if partial.get("errorMessage"):
            print(f"  run {i + 1}: ACCEPTED-BUT-DROPPED -> {partial['errorMessage']}",
                  file=sys.stderr)
        else:
            sent += 1
            print(f"  run {i + 1}/{runs}: ok  stored={parsed.get('stored', '?')}"
                  f"  otelTraceId={trace_id[:12]}…")

    print(f"\n{sent}/{runs} export(s) accepted at {endpoint}")
    if sent == 0:
        print("Nothing landed. The endpoint returns 200 even when it drops data — "
              "check the partialSuccess messages above and your gateway key.",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=("opik", "aura", "otlp"), default="opik",
                    help="opik = local Opik API; aura = Aura's Opik-compatible path; "
                         "otlp = Aura's raw OTLP receiver")
    ap.add_argument("--url", default="http://localhost:5173/api/",
                    help="Opik API base for opik/aura, or the Aura host for otlp")
    ap.add_argument("--api-key", default=os.getenv("AURA_GATEWAY_KEY", ""),
                    help="gw- gateway key. Not needed against a local OSS Opik.")
    ap.add_argument("--project", default="sample-agent")
    ap.add_argument("--runs", type=int, default=4,
                    help="How many traces to send. The last one fails on purpose.")
    args = ap.parse_args()

    print(f"target={args.target}  url={args.url}  project={args.project}  "
          f"runs={args.runs}")

    if args.target == "otlp":
        return run_via_otlp(args.url, args.api_key, args.project, args.runs)

    url = args.url
    if args.target == "aura" and "/opik/api" not in url:
        # Aura fronts Opik at /opik/api/; the SDK needs that full base.
        url = f"{url.rstrip('/')}/opik/api/"
        print(f"  (rewrote url to {url})")
    return run_via_sdk(url, args.api_key, args.project, args.runs)


if __name__ == "__main__":
    sys.exit(main())
