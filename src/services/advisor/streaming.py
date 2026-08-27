import json
from fastapi import WebSocket


async def send_token(ws: WebSocket, content: str) -> None:
    await ws.send_text(json.dumps({"type": "token", "content": content}))


async def send_tool_call(ws: WebSocket, name: str, input: dict) -> None:
    await ws.send_text(json.dumps({"type": "tool_call", "name": name, "input": input}))


async def send_tool_result(ws: WebSocket, name: str, output: dict) -> None:
    await ws.send_text(json.dumps({"type": "tool_result", "name": name, "output": output}))


async def send_done(ws: WebSocket, session_id: str) -> None:
    await ws.send_text(json.dumps({"type": "done", "session_id": session_id}))


async def send_error(ws: WebSocket, message: str) -> None:
    await ws.send_text(json.dumps({"type": "error", "message": message}))


async def send_usage(ws: WebSocket, input_tokens: int, output_tokens: int, cost: float) -> None:
    await ws.send_text(json.dumps({
        "type": "usage",
        "input": input_tokens,
        "output": output_tokens,
        "cost": cost,
    }))
