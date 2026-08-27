import json
import logging
from typing import Generator

logger = logging.getLogger(__name__)


def invoke_streaming(
    messages: list[dict],
    system_prompt: str,
    tools_def: list[dict],
    model_id: str,
    api_key: str,
) -> Generator[dict, None, None]:
    """
    Invoke Claude via Anthropic API and yield structured events.
    Same output format as bedrock_client.invoke_streaming.
    """
    try:
        import anthropic
    except ImportError:
        yield {"type": "error", "message": "anthropic package not installed. Run: pip install anthropic"}
        return

    client = anthropic.Anthropic(api_key=api_key or None)

    try:
        response = client.messages.create(
            model=model_id,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
            tools=tools_def,
        )
    except Exception as exc:
        logger.error("Anthropic API invocation failed: %s", exc)
        yield {"type": "error", "message": str(exc)}
        return

    for block in response.content:
        if block.type == "text":
            # Emit in small word chunks to simulate streaming to the frontend
            words = block.text.split(" ")
            chunk_size = 6
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i : i + chunk_size])
                if i + chunk_size < len(words):
                    chunk += " "
                yield {"type": "text", "content": chunk}
        elif block.type == "tool_use":
            yield {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }

    # Emit token usage so the orchestrator can persist and relay it
    if hasattr(response, "usage") and response.usage:
        yield {
            "type": "usage",
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        }

    yield {"type": "end", "stop_reason": response.stop_reason}
