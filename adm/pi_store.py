"""Read-only Pi coding-agent session store.

Pi writes one ``*.jsonl`` per session under
``~/.pi/agent/sessions/<project>/``.  The first record is a ``session``
header; ``message`` records carry Claude-style content blocks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def sessions_dir() -> Path | None:
    env = (os.environ.get("PI_AGENT_HOME")
           or os.environ.get("PI_CODING_AGENT_DIR")
           or os.environ.get("PI_HOME"))
    if env:
        p = Path(env).expanduser()
        cands = [p / "sessions", p / "agent" / "sessions", p]
        for c in cands:
            if c.is_dir():
                return c
        return p / "agent" / "sessions"
    p = Path.home() / ".pi" / "agent" / "sessions"
    return p if p.is_dir() else None


def _blocks(content) -> list[Part]:
    parts: list[Part] = []
    if isinstance(content, str):
        if content.strip():
            parts.append(Part(kind="text", text=content))
        return parts
    if not isinstance(content, list):
        return parts
    for b in content:
        if not isinstance(b, dict):
            continue
        typ = b.get("type")
        if typ == "text":
            parts.append(Part(kind="text", text=b.get("text") or ""))
        elif typ in ("thinking", "reasoning"):
            parts.append(Part(kind="thinking",
                              text=b.get("thinking") or b.get("text") or ""))
        elif typ in ("tool_use", "toolCall", "tool_call"):
            name = b.get("name") or b.get("toolName") or "tool"
            args = b.get("input") or b.get("arguments") or b.get("args") or {}
            parts.append(Part(kind="tool_call", name=str(name),
                              text=_pretty(args), tool_id=b.get("id") or "",
                              raw=b))
        elif typ in ("tool_result", "toolResult"):
            text = b.get("content") or b.get("output") or b.get("result") or ""
            parts.append(Part(kind="tool_result", text=_flat(text),
                              tool_id=b.get("tool_use_id") or b.get("id") or "",
                              raw=b))
    return parts


def _flat(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flat(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "output", "result"):
            if key in value:
                return _flat(value[key])
        return _pretty(value)
    return "" if value is None else str(value)


def _pretty(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _scan_file(path: Path) -> SessionMeta | None:
    try:
        st = path.stat()
    except OSError:
        return None
    sid = path.stem
    cwd = ""
    created = None
    updated = None
    model = ""
    n_user = n_asst = 0
    first_user = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                typ = o.get("type")
                ts = util.parse_iso(o.get("timestamp"))
                if typ == "session":
                    sid = str(o.get("id") or sid)
                    cwd = util.strip_verbatim(str(o.get("cwd") or ""))
                    created = ts or created
                elif typ == "model_change":
                    model = str(o.get("modelId") or o.get("model") or model)
                elif typ == "message":
                    if ts:
                        created = created or ts
                        updated = ts
                    msg = o.get("message") or {}
                    role = msg.get("role")
                    if role == "user":
                        n_user += 1
                        if not first_user:
                            first_user = _text_of(msg.get("content"))
                    elif role == "assistant":
                        if msg.get("content") or msg.get("toolCalls"):
                            n_asst += 1
                    if not model:
                        model = str(msg.get("model") or model)
    except OSError:
        return None
    title = util.one_line(first_user, 80) or sid
    return SessionMeta(
        source="pi", id=sid, path=str(path),
        project=util.path_to_display(cwd or str(path.parent)) or "Pi",
        cwd=cwd, title=title, n_messages=n_user + n_asst, n_user=n_user,
        n_assistant=n_asst, size=st.st_size, created=created,
        updated=updated or created, model=model,
        preview=util.one_line(first_user, 120),
    )


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return str(b.get("text") or "")
    return ""


def scan_pi(progress=None, cache=None) -> list[SessionMeta]:
    base = sessions_dir()
    if not base:
        return []
    if progress:
        progress("Pi sessions…")
    files = sorted(base.rglob("*.jsonl"))
    metas: list[SessionMeta] = []
    seen: set[str] = set()
    for n, f in enumerate(files):
        try:
            st = f.stat()
        except OSError:
            continue
        seen.add(str(f))
        m = cache.get_meta("pi", str(f), st.st_mtime_ns, st.st_size) \
            if cache is not None else None
        if m is None:
            m = _scan_file(f)
            if m is None:
                continue
            if cache is not None:
                cache.put_meta("pi", str(f), st.st_mtime_ns, st.st_size, m)
        metas.append(m)
        if progress and n % 20 == 0:
            progress(f"Pi {n + 1}/{len(files)}")
    if cache is not None:
        cache.prune_meta("pi", seen)
    return metas


def load_session(meta: SessionMeta) -> LoadedSession:
    path = Path(meta.path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        raw = []
    messages: list[Message] = []
    model = meta.model
    for i, line in enumerate(raw):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        typ = o.get("type")
        if typ == "model_change":
            model = str(o.get("modelId") or o.get("model") or model)
            continue
        if typ != "message":
            continue
        msg = o.get("message") or {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        parts = _blocks(msg.get("content"))
        if not parts:
            continue
        if role == "assistant" and model:
            msg_model = str(msg.get("model") or model)
        else:
            msg_model = str(msg.get("model") or "")
        messages.append(Message(
            line=i, role=role, ts=util.parse_iso(o.get("timestamp")),
            parts=parts, model=msg_model or None))
    return LoadedSession(meta=meta, messages=messages, raw_lines=raw,
                         total_lines=len(raw))
