"""Global full-text search across all session files (raw jsonl substring)."""
from __future__ import annotations

import json

from PyQt6.QtCore import QThread, pyqtSignal

from . import sources
from .models import SessionMeta

MAX_HITS = 3000


class SearchThread(QThread):
    """Searches raw jsonl lines so it also finds tool args / file paths.

    Sessions store text as non-escaped UTF-8, so a plain substring match on
    the raw line covers everything visible in the viewer.
    """
    hit = pyqtSignal(dict)      # {meta, line, role, snippet, text}
    done = pyqtSignal(int)      # total hits
    status = pyqtSignal(str)

    def __init__(self, metas: list[SessionMeta], query: str, parent=None):
        super().__init__(parent)
        self.metas = metas
        self.query = query.strip()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        q = self.query.lower()
        n = 0
        for k, m in enumerate(self.metas):
            if self._cancel or n >= MAX_HITS or not q:
                break
            self.status.emit(f"{k + 1}/{len(self.metas)} {m.display_title[:40]}")
            if m.source not in ("claude", "codex"):
                try:
                    sess = sources.load_session(m)
                except Exception:  # noqa: BLE001 — one bad store is non-fatal
                    continue
                for msg in sess.messages:
                    text = "\n".join(p.text for p in msg.parts if p.text)
                    if q not in text.lower():
                        continue
                    pos = text.lower().find(q)
                    self.hit.emit({
                        "meta": m, "line": msg.line, "role": msg.role,
                        "text": text,
                        "snippet": _snippet(text, max(0, pos)),
                    })
                    n += 1
                    if n >= MAX_HITS:
                        break
                if n >= MAX_HITS:
                    break
                continue
            try:
                with open(m.path, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if q in line.lower():
                            role, text = _extract(line)
                            if text is None:
                                continue
                            pos = text.lower().find(q)
                            if pos == -1:
                                pos = 0
                            self.hit.emit({
                                "meta": m, "line": i, "role": role,
                                "text": text,
                                "snippet": _snippet(text, pos),
                            })
                            n += 1
                            if n >= MAX_HITS:
                                break
            except OSError:
                continue
        self.done.emit(n)


def _extract(line: str) -> tuple[str, str | None]:
    try:
        o = json.loads(line)
    except ValueError:
        return "", None
    t = o.get("type")
    if t == "response_item":
        p = o.get("payload") or {}
        pt = p.get("type")
        if pt == "message":
            texts = [x.get("text", "") for x in (p.get("content") or [])
                     if isinstance(x, dict)]
            return p.get("role") or "user", "\n".join(texts)
        if pt in ("function_call", "function_call_output", "reasoning"):
            blob = p.get("arguments") or p.get("output") or ""
            if not isinstance(blob, str):
                blob = json.dumps(blob, ensure_ascii=False)
            return pt, blob
        return "", None
    if t in ("user", "assistant"):
        msg = o.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            return t, c
        if isinstance(c, list):
            texts = []
            for x in c:
                if isinstance(x, dict):
                    if x.get("type") == "text":
                        texts.append(x.get("text", ""))
                    elif x.get("type") == "tool_use":
                        texts.append(json.dumps(x.get("input"), ensure_ascii=False))
                    elif x.get("type") == "tool_result":
                        texts.append(_flat(x.get("content")))
            return t, "\n".join(texts)
    return "", None


def _flat(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def _snippet(text: str, pos: int, before: int = 60, after: int = 220) -> str:
    s = max(0, pos - before)
    frag = " ".join(text[s: pos + after].split())
    return ("…" if s > 0 else "") + frag
