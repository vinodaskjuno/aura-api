"""Encrypted, file-based, tiered conversation memory — no database.

Layout under backend/memory/sessions/<session_id>/ :
  active.json.enc          working memory: recent Bedrock messages + structured state
  archival/<ts>.json.enc   swept, summarized older memories (keywords + importance)
  index.json.enc           lightweight catalog of archival records for retrieval

All files are Fernet-encrypted at rest (AES-128-CBC + HMAC) and only decrypted in
memory during a conversation. Nothing plaintext is ever written to disk.
"""
from __future__ import annotations

import base64
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .. import config

_STOPWORDS = {
    "the", "and", "for", "are", "with", "that", "this", "have", "from", "what",
    "which", "list", "show", "give", "does", "their", "them", "all", "any",
    "into", "over", "your", "you", "our", "was", "were", "has", "had",
}


def _fernet() -> Fernet:
    """Resolve the encryption key: explicit key > passphrase-derived > auto-generated.

    Auto-generation keeps the app working with zero config (DB-free, local-only),
    persisting a key file inside memory/ that is git-ignored.
    """
    if config.MEMORY_ENCRYPTION_KEY:
        return Fernet(config.MEMORY_ENCRYPTION_KEY.encode())

    config.ensure_dirs()
    salt_path = config.MEMORY_DIR / ".salt"
    if config.MEMORY_PASSPHRASE:
        if salt_path.exists():
            salt = salt_path.read_bytes()
        else:
            salt = base64.urlsafe_b64encode(Fernet.generate_key())[:16]
            salt_path.write_bytes(salt)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390000)
        key = base64.urlsafe_b64encode(kdf.derive(config.MEMORY_PASSPHRASE.encode()))
        return Fernet(key)

    key_path = config.MEMORY_DIR / ".key"
    if key_path.exists():
        return Fernet(key_path.read_bytes())
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    return key and Fernet(key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _keywords(text: str, top: int = 12) -> list[str]:
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,}", text.lower())
             if w not in _STOPWORDS]
    return [w for w, _ in Counter(words).most_common(top)]


def _approx_tokens(obj) -> int:
    return len(json.dumps(obj)) // 4


class SessionMemory:
    def __init__(self, session_id: str, summarizer: Optional[Callable[[str], str]] = None):
        self.session_id = re.sub(r"[^A-Za-z0-9_\-]", "_", session_id) or "default"
        self._f = _fernet()
        self._summarize = summarizer
        self.base = config.MEMORY_DIR / "sessions" / self.session_id
        self.archival_dir = self.base / "archival"
        self.active_path = self.base / "active.json.enc"
        self.index_path = self.base / "index.json.enc"
        self.messages: list[dict] = []
        self.state: dict = {"mapped_folder": None, "build_stats": None,
                            "prior_questions": [], "recommendations_surfaced": []}
        self._load()

    # --- crypto io --------------------------------------------------------
    def _write(self, path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._f.encrypt(json.dumps(obj).encode("utf-8")))

    def _read(self, path: Path):
        if not path.exists():
            return None
        return json.loads(self._f.decrypt(path.read_bytes()).decode("utf-8"))

    # --- lifecycle --------------------------------------------------------
    def _load(self) -> None:
        data = self._read(self.active_path)
        if data:
            self.messages = data.get("messages", [])
            self.state.update(data.get("state", {}))

    def save(self) -> None:
        self._write(self.active_path, {"messages": self.messages, "state": self.state,
                                       "updated": _now()})

    # --- state helpers ----------------------------------------------------
    def note_build(self, mapped_folder: str, build_stats: dict) -> None:
        self.state["mapped_folder"] = mapped_folder
        self.state["build_stats"] = build_stats
        self.save()

    def note_question(self, q: str) -> None:
        self.state["prior_questions"] = (self.state.get("prior_questions", []) + [q])[-20:]

    def add_message(self, message: dict) -> None:
        self.messages.append(message)

    # --- sweep to archival ------------------------------------------------
    def maybe_sweep(self) -> Optional[dict]:
        """When active memory grows past thresholds, summarize the oldest chunk into
        an encrypted archival record and drop it from the live buffer.
        """
        too_many = len(self.messages) > config.MEMORY_MAX_ACTIVE_TURNS * 2
        too_big = _approx_tokens(self.messages) > config.MEMORY_MAX_ACTIVE_TOKENS
        if not (too_many or too_big):
            return None

        keep = config.MEMORY_MAX_ACTIVE_TURNS  # keep the most recent N messages live
        old, self.messages = self.messages[:-keep], self.messages[-keep:]
        if not old:
            return None

        transcript = _messages_to_text(old)
        summary = (self._summarize(transcript) if self._summarize
                   else transcript[:1500])
        ts = _now()
        record = {"ts": ts, "summary": summary, "keywords": _keywords(summary + " " + transcript),
                  "importance": _importance(transcript), "messages": len(old)}
        self._write(self.archival_dir / f"{ts.replace(':', '-')}.json.enc", record)

        index = self._read(self.index_path) or []
        index.append({"ts": ts, "keywords": record["keywords"],
                      "importance": record["importance"],
                      "file": f"{ts.replace(':', '-')}.json.enc"})
        self._write(self.index_path, index)
        self.save()
        return record

    # --- recall -----------------------------------------------------------
    def retrieve(self, query: str, k: int = 3) -> list[dict]:
        index = self._read(self.index_path) or []
        if not index:
            return []
        qk = set(_keywords(query, top=20))
        scored = []
        for i, entry in enumerate(index):
            overlap = len(qk & set(entry.get("keywords", [])))
            recency = i / max(1, len(index))          # newer = higher
            importance = entry.get("importance", 0) / 5.0
            score = overlap * 2 + recency + importance
            scored.append((score, entry))
        scored.sort(key=lambda t: -t[0])
        out = []
        for score, entry in scored[:k]:
            if score <= 0:
                continue
            rec = self._read(self.archival_dir / entry["file"])
            if rec:
                out.append(rec)
        return out

    def get_context_summary(self, query: str = "") -> str:
        """Rendered block injected into the system prompt each turn."""
        lines = []
        st = self.state
        if st.get("mapped_folder"):
            lines.append(f"- Mapped data folder: {st['mapped_folder']}")
        if st.get("build_stats"):
            bs = st["build_stats"]
            lines.append(f"- Ontology built: {bs.get('nodes','?')} nodes / "
                         f"{bs.get('edges','?')} edges, {bs.get('recommendations','?')} recommendations.")
        if st.get("prior_questions"):
            lines.append("- Earlier questions this session: "
                         + "; ".join(st["prior_questions"][-5:]))
        for rec in self.retrieve(query):
            lines.append(f"- [earlier, {rec['ts'][:10]}] {rec['summary']}")
        if not lines:
            return ""
        return "SESSION MEMORY (what happened earlier):\n" + "\n".join(lines)


def _messages_to_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and "text" in c]
            content = " ".join(texts)
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _importance(text: str) -> int:
    t = text.lower()
    score = 1
    for kw in ("critical", "unencrypted", "internet", "vulnerability", "modernize",
               "recommendation", "production", "retire", "refactor"):
        if kw in t:
            score += 1
    return min(score, 5)
