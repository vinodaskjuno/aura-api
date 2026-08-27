"""AWS Bedrock streaming client (Claude via the Converse API).

Credentials are resolved from ~/.aws/credentials via the standard boto3 chain.
Set AWS_PROFILE in config/{env}.env to select a named profile.
Supports tool config and a cooperative abort flag. Sync generator;
the server runs it in a worker thread and relays events to the WebSocket.
"""
from __future__ import annotations

import json as _json
import logging
import threading
from typing import Iterator, Optional

import boto3
import botocore
from botocore.config import Config

from .. import config

log = logging.getLogger(__name__)


class BedrockError(RuntimeError):
    pass


class BedrockClient:
    def __init__(self) -> None:
        self._client = None
        self.model_id = config.BEDROCK_MODEL_ID

    def _get(self):
        if self._client is not None:
            return self._client

        creds = config.load_bedrock_credentials()
        self.model_id = creds.get("model_id") or config.BEDROCK_MODEL_ID
        profile = config.AWS_PROFILE or None
        region = creds.get("region") or config.AWS_REGION

        log.info(
            "BedrockClient init | profile=%s region=%s model=%s botocore=%s",
            profile or "(default chain)",
            region,
            self.model_id,
            botocore.__version__,
        )

        try:
            session = boto3.Session(profile_name=profile, region_name=region)
            resolved = session.get_credentials()
            if resolved:
                frozen = resolved.get_frozen_credentials()
                log.info(
                    "Credentials resolved | key_id=%s... token_present=%s",
                    (frozen.access_key or "")[:8],
                    bool(frozen.token),
                )
            else:
                log.warning("No AWS credentials resolved — Bedrock calls will fail")
        except Exception as e:
            log.error("boto3.Session creation failed: %s", e)
            raise BedrockError(f"AWS session error: {e}") from e

        self._client = session.client(
            "bedrock-runtime",
            config=Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 1}),
            verify=not config.BEDROCK_DISABLE_SSL_VERIFY,
        )
        log.info("Bedrock runtime client created (region=%s)", region)
        return self._client

    def converse_stream(
        self,
        messages: list[dict],
        system_text: str,
        tools: Optional[list[dict]] = None,
        abort: Optional[threading.Event] = None,
        max_tokens: int = 4096,
    ) -> Iterator[dict]:
        """Yield normalized events:
            {"type":"token","text":...}
            {"type":"tool_use","id","name","input"}   (assembled)
            {"type":"stop","reason":...}
            {"type":"usage","input","output"}
        """
        client = self._get()

        system: list[dict] = [{"text": system_text}]

        req = {
            "modelId": self.model_id,
            "messages": messages,
            "system": system,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
        }
        if tools:
            req["toolConfig"] = {"tools": tools}

        log.info(
            "converse_stream | model=%s system_chars=%d messages=%d tools=%d",
            self.model_id,
            len(system_text),
            len(messages),
            len(tools) if tools else 0,
        )

        try:
            resp = client.converse_stream(**req)
        except Exception as e:
            err = str(e)
            # cachePoint is only accepted by models that support prompt caching.
            # If the model rejects it, retry once without it.
            if "cachePoint" in err and len(system) > 1:
                log.info("Model does not support cachePoint — retrying without it")
                req["system"] = [{"text": system_text}]
                try:
                    resp = client.converse_stream(**req)
                except Exception as e2:
                    raise BedrockError(str(e2)) from e2
            else:
                log.error(
                    "converse_stream API error | model=%s error=%s",
                    self.model_id, e, exc_info=True,
                )
                raise BedrockError(err) from e

        blocks: dict[int, dict] = {}
        stop_reason = "end_turn"
        token_count = 0

        for event in resp["stream"]:
            if abort and abort.is_set():
                log.info("Stream aborted by client after %d tokens", token_count)
                stop_reason = "aborted"
                break
            if "contentBlockStart" in event:
                start = event["contentBlockStart"]
                idx = start["contentBlockIndex"]
                tu = start.get("start", {}).get("toolUse")
                if tu:
                    log.debug("Tool use start | idx=%d tool=%s id=%s", idx, tu["name"], tu["toolUseId"])
                    blocks[idx] = {"type": "tool_use", "id": tu["toolUseId"],
                                   "name": tu["name"], "input": ""}
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    token_count += 1
                    yield {"type": "token", "text": delta["text"]}
                elif "toolUse" in delta:
                    idx = event["contentBlockDelta"]["contentBlockIndex"]
                    blocks.setdefault(idx, {"type": "tool_use", "id": "", "name": "", "input": ""})
                    blocks[idx]["input"] += delta["toolUse"].get("input", "")
            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", stop_reason)
                log.info("Stream complete | stop_reason=%s tokens_streamed=%d", stop_reason, token_count)
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                log.info(
                    "Token usage | input=%s output=%s",
                    usage.get("inputTokens"), usage.get("outputTokens"),
                )
                yield {"type": "usage", "input": usage.get("inputTokens"),
                       "output": usage.get("outputTokens")}

        # Emit assembled tool-use blocks after the stream.
        for b in blocks.values():
            if b.get("type") == "tool_use":
                raw = b.get("input") or ""
                try:
                    parsed = _json.loads(raw) if raw.strip() else {}
                except _json.JSONDecodeError:
                    log.warning("Failed to parse tool input JSON for tool=%s raw=%r", b["name"], raw[:200])
                    parsed = {}
                log.debug("Tool use emit | tool=%s input_keys=%s", b["name"], list(parsed.keys()))
                yield {"type": "tool_use", "id": b["id"], "name": b["name"], "input": parsed}

        yield {"type": "stop", "reason": stop_reason}
