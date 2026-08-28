"""Diagnose the configured LLM backend end to end.

    python -m src.scripts.check_llm

Answers the two questions that look identical from the application log:
  * is the API key valid?              -> GET /v1/models (auth only, no credit used)
  * can it actually run a completion?  -> POST /v1/messages (needs credit)

A key can be perfectly valid and still fail every request when the workspace it
belongs to is out of credit, which is why checking auth alone is not enough.
"""
from __future__ import annotations

import os
import sys

from src.config_settings import get_settings


def _fp(secret: str) -> str:
    if not secret:
        return "(empty)"
    return f"{secret[:14]}…{secret[-4:]}  (len {len(secret)})"


def _line(label: str, value: object) -> None:
    print(f"  {label:<26} {value}")


def _probe_profiles(s) -> None:
    """List Anthropic inference profiles in this region that actually respond.

    Listing profiles is not enough — this account can *see* opus-5 and opus-4-8 but
    is denied on both. Only an actual invocation distinguishes visible from usable,
    so each candidate gets one 8-token call.
    """
    import boto3
    from src.observability.llm import _invoke_bedrock

    print("\n  Probing which Anthropic inference profiles work here…")
    try:
        client = boto3.client("bedrock", region_name=s.bedrock_region)
        summaries = client.list_inference_profiles().get("inferenceProfileSummaries", [])
    except Exception as exc:                                        # noqa: BLE001
        print(f"    could not list inference profiles: {str(exc)[:120]}")
        return

    ids = sorted({p["inferenceProfileId"] for p in summaries
                  if "anthropic" in p.get("inferenceProfileId", "")})
    # Newest first; older Claude 3.x generations are not worth a paid probe.
    candidates = [i for i in reversed(ids)
                  if not any(old in i for old in ("claude-3-", "claude-v2", "instant"))][:8]

    working: list[str] = []
    for mid in candidates:
        try:
            _invoke_bedrock("Reply with OK.", "ping", mid, 8)
            working.append(mid)
            print(f"    WORKS  {mid}")
        except Exception:                                           # noqa: BLE001
            pass

    if working:
        print(f"\n    Set one of these in src/.env, then restart:")
        print(f"      BEDROCK_MODEL_ID={working[0]}")
    else:
        print("    None responded — no Anthropic model is enabled for this account "
              "in this region.")


def main() -> int:
    get_settings.cache_clear()
    s = get_settings()

    print("\n── Configuration ────────────────────────────────────────────────")
    _line("LLM_BACKEND", s.llm_backend)
    _line("ANTHROPIC_MODEL_ID", s.anthropic_model_id)
    _line("BEDROCK_MODEL_ID", s.bedrock_model_id)
    _line("anthropic key", _fp(s.anthropic_api_key))

    # A real environment variable OUTRANKS the .env file in pydantic-settings, so an
    # exported key silently wins over the one you just edited. This is the single
    # most common reason a .env change appears to have no effect.
    shadow = os.environ.get("ANTHROPIC_API_KEY")
    if shadow:
        same = shadow.strip() == (s.anthropic_api_key or "").strip()
        _line("shell env var", f"SET {_fp(shadow)}")
        if not same:
            print("\n  !! An exported ANTHROPIC_API_KEY is SHADOWING src/.env.")
            print("     pydantic-settings reads real env vars before the env file.")
            print("     Fix: unset ANTHROPIC_API_KEY   (then restart the server)")
    else:
        _line("shell env var", "not set (.env wins — good)")

    failures = 0

    if s.llm_backend == "anthropic":
        print("\n── Anthropic ────────────────────────────────────────────────────")
        if not s.anthropic_api_key:
            print("  ANTHROPIC_API_KEY is empty."); return 1
        import anthropic
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)

        # 1. Auth — free.
        try:
            models = client.models.list(limit=1)
            _line("auth (GET /v1/models)", "OK — key is valid")
            ids = [m.id for m in client.models.list(limit=100).data]
            _line("models visible", len(ids))
            if s.anthropic_model_id in ids:
                _line("configured model", f"{s.anthropic_model_id}  available")
            else:
                _line("configured model", f"{s.anthropic_model_id}  NOT in the list")
                print(f"     available: {', '.join(ids[:8])}")
                failures += 1
        except Exception as exc:                                    # noqa: BLE001
            _line("auth (GET /v1/models)", f"FAILED — {exc}")
            print("\n  The key itself is rejected. It is wrong, revoked, or truncated.")
            return 1

        # 2. Completion — costs a fraction of a cent.
        try:
            r = client.messages.create(
                model=s.anthropic_model_id, max_tokens=8,
                messages=[{"role": "user", "content": "Reply with OK."}])
            text = "".join(getattr(b, "text", "") for b in r.content)
            _line("completion", f"OK — {text.strip()!r}")
            print("\n  Anthropic is fully working.")
        except Exception as exc:                                    # noqa: BLE001
            body = getattr(exc, "body", None)
            msg = (body or {}).get("error", {}).get("message", str(exc)) \
                if isinstance(body, dict) else str(exc)
            _line("completion", f"FAILED — {msg}")
            failures += 1
            if "credit balance" in msg.lower():
                print("\n  The KEY IS VALID — auth succeeded above. The workspace it")
                print("  belongs to has no credit. A new key from the SAME workspace")
                print("  will fail identically; credit is billed per workspace, not")
                print("  per key. Either add credit at console.anthropic.com/settings/billing,")
                print("  create a key in a funded workspace, or set LLM_BACKEND=bedrock.")

    if s.llm_backend == "bedrock" or failures:
        header = ("Bedrock" if s.llm_backend == "bedrock"
                  else "Bedrock (alternative backend)")
        print(f"\n── {header} ────────────────────────────────────────────")
        from src.observability.llm import _invoke_bedrock, profile_prefix

        # Pre-flight: a bare foundation-model id is rejected before any call is made,
        # so name it here rather than let the operator read an AWS ValidationException.
        if not s.bedrock_model_id.startswith(("us.", "eu.", "apac.", "arn:")):
            want = profile_prefix(s.bedrock_region) + s.bedrock_model_id.lstrip(".")
            _line("model id", f"{s.bedrock_model_id}  ← bare foundation-model id")
            print(f"     Current Anthropic models need an INFERENCE PROFILE.")
            print(f"     Set BEDROCK_MODEL_ID={want}")
            failures += 1

        try:
            text, _ = _invoke_bedrock("Reply with OK.", "ping", s.bedrock_model_id, 8)
            _line(s.bedrock_model_id, f"OK — {text.strip()!r}")
            if s.llm_backend != "bedrock":
                print("\n  Bedrock works. Set LLM_BACKEND=bedrock in src/.env and restart.")
        except Exception as exc:                                    # noqa: BLE001
            _line(s.bedrock_model_id, f"FAILED — {str(exc)[:180]}")
            failures += 1
            _probe_profiles(s)

    print("\n  NOTE: settings are cached at import (@lru_cache on get_settings), so a")
    print("  .env edit needs a server RESTART — --reload does not always notice it.\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
