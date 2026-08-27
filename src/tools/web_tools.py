"""Web tools — Tier 3 (web_search, fetch_url)."""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_FETCH_CAP = 10_000
_TIMEOUT = 10


def t_web_search(query: str) -> dict:
    """Search via DuckDuckGo Instant Answer API (no API key required)."""
    try:
        params = urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
        url = f"https://api.duckduckgo.com/?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "AURA-DevBot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return {"error": f"Search failed: {e}", "query": query}

    results: list[dict] = []

    # Abstract (direct answer)
    if data.get("AbstractText"):
        results.append({
            "type": "abstract",
            "title": data.get("Heading", query),
            "snippet": data["AbstractText"][:500],
            "url": data.get("AbstractURL", ""),
        })

    # Related topics
    for topic in data.get("RelatedTopics", [])[:6]:
        if isinstance(topic, dict) and topic.get("Text"):
            results.append({
                "type": "result",
                "title": topic.get("FirstURL", "").split("/")[-1].replace("_", " "),
                "snippet": topic["Text"][:300],
                "url": topic.get("FirstURL", ""),
            })

    # Results array (web results)
    for item in data.get("Results", [])[:4]:
        results.append({
            "type": "web",
            "title": item.get("Text", "")[:100],
            "snippet": item.get("Text", "")[:300],
            "url": item.get("FirstURL", ""),
        })

    if not results:
        return {
            "query": query,
            "count": 0,
            "message": "No results found. Try a more specific query.",
        }

    return {"query": query, "count": len(results), "results": results}


def t_fetch_url(url: str) -> dict:
    """Fetch a URL and return cleaned text content."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AURA-DevBot/1.0 (documentation fetch)",
                "Accept": "text/html,text/plain,application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(_FETCH_CAP * 4)  # read extra for HTML stripping
            charset = "utf-8"
            ct = resp.headers.get_content_charset()
            if ct:
                charset = ct
            content = raw.decode(charset, errors="replace")
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "url": url}
    except urllib.error.URLError as e:
        return {"error": f"URL error: {e.reason}", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}

    # Strip HTML if needed
    if "html" in content_type.lower():
        content = _strip_html(content)

    content = content[:_FETCH_CAP]
    return {
        "url": url,
        "content_type": content_type.split(";")[0].strip(),
        "content": content,
        "truncated": len(content) >= _FETCH_CAP,
    }


def _strip_html(html_text: str) -> str:
    """Naive but effective HTML stripper."""
    # Remove script and style blocks entirely
    text = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html_text,
                  flags=re.DOTALL | re.IGNORECASE)
    # Remove all other tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode HTML entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()
