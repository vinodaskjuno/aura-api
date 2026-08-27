import hashlib
import base64

from cryptography.fernet import Fernet

_sessions: dict[str, list[dict]] = {}
MAX_TURNS = 12


def _get_fernet(secret: str) -> Fernet:
    key = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def append_turn(session_id: str, role: str, content: str, secret: str = "ontoverse") -> None:
    if session_id not in _sessions:
        _sessions[session_id] = []
    _sessions[session_id].append({"role": role, "content": content})
    if len(_sessions[session_id]) > MAX_TURNS * 2:
        _sessions[session_id] = _sessions[session_id][-(MAX_TURNS * 2):]


def get_history(session_id: str) -> list[dict]:
    return _sessions.get(session_id, [])


def clear_session(session_id: str) -> None:
    _sessions.pop(session_id, None)
