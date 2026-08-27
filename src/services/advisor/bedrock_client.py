import json
import logging
from typing import Generator, Any

import boto3

logger = logging.getLogger(__name__)


def invoke_streaming(
    messages: list[dict],
    system_prompt: str,
    tools_def: list[dict],
    model_id: str,
    region: str,
) -> Generator[dict, None, None]:
    """
    Invoke Claude via AWS Bedrock with streaming and yield structured events.

    Yields dicts of the form:
      {"type": "text",     "content": "..."}
      {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
      {"type": "end",      "stop_reason": "..."}
    """
    client = boto3.client("bedrock-runtime", region_name=region)

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
            "tools": tools_def,
        }
    )

    try:
        response = client.invoke_model_with_response_stream(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
    except Exception as exc:
        logger.error("Bedrock invocation failed: %s", exc)
        yield {"type": "error", "message": str(exc)}
        return

    stream = response.get("body")
    if not stream:
        yield {"type": "end", "stop_reason": "no_stream"}
        return

    stop_reason = "end_turn"
    invocation_metrics: dict = {}
    # Track active tool-use blocks: {index: {id, name, input_json_parts}}
    tool_blocks: dict[int, dict[str, Any]] = {}

    for event in stream:
        chunk = event.get("chunk")
        if not chunk:
            continue

        try:
            raw = json.loads(chunk["bytes"].decode("utf-8"))
        except (KeyError, json.JSONDecodeError) as exc:
            logger.warning("Failed to decode chunk: %s", exc)
            continue

        event_type = raw.get("type", "")

        if event_type == "content_block_start":
            block = raw.get("content_block", {})
            index = raw.get("index", 0)
            if block.get("type") == "tool_use":
                tool_blocks[index] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input_parts": [],
                }

        elif event_type == "content_block_delta":
            index = raw.get("index", 0)
            delta = raw.get("delta", {})
            delta_type = delta.get("type", "")

            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    yield {"type": "text", "content": text}

            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json", "")
                if index in tool_blocks:
                    tool_blocks[index]["input_parts"].append(partial)

        elif event_type == "content_block_stop":
            index = raw.get("index", 0)
            if index in tool_blocks:
                block = tool_blocks.pop(index)
                raw_input = "".join(block["input_parts"])
                try:
                    parsed_input = json.loads(raw_input) if raw_input else {}
                except json.JSONDecodeError:
                    parsed_input = {"_raw": raw_input}
                yield {
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": parsed_input,
                }

        elif event_type == "message_delta":
            delta = raw.get("delta", {})
            if "stop_reason" in delta:
                stop_reason = delta["stop_reason"]

        elif event_type == "message_stop":
            metrics = raw.get("amazon-bedrock-invocationMetrics", {})
            invocation_metrics = metrics
            stop_reason = metrics.get("stopReason", stop_reason)

    # Emit token usage before the end event
    if invocation_metrics:
        yield {
            "type": "usage",
            "input": invocation_metrics.get("inputTokenCount", 0),
            "output": invocation_metrics.get("outputTokenCount", 0),
        }

    yield {"type": "end", "stop_reason": stop_reason}
