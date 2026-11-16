"""Read-only OpenCode (2.x) session store.

OpenCode keeps sessions in ``opencode.db`` (SQLite).  The desktop/web UI is
backed by the same database, so this works without a running service.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def db_path() -> Path | None:
    env = os.environ.get("OPENCODE_DATA_DIR")
    cands = []
    if env:
        cands.append(Path(env) / "opencode.db")
    base = Path(os.environ.get("XDG_DATA_HOME")
                or Path.home() / ".local" / "share")
    cands.append(base / "opencode" / "opencode.db")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "opencode" / "opencode.db")
    for p in cands:
        if p.is_file():
            return p
    return None


def _connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=4)
    con.row_factory = sqlite3.Row
    return con


def _ms(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _model_name(raw) -> str:
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return str(data.get("id") or "")
    except (TypeError, ValueError):
        return ""


def _first_user(data_raw) -> str:
    try:
        data = json.loads(data_raw or "{}")
    except (TypeError, ValueError):
        return ""
    text = data.get("text")
    if isinstance(text, str):
        return text
    content = data.get("content")
    if isinstance(content, list):
        return " ".join(str(x.get("text") or "") for x in content
                        if isinstance(x, dict))
    return ""


def scan_opencode(progress=None, cache=None) -> list[SessionMeta]:
    path = db_path()
    if not path:
        return []
    if progress:
        progress("OpenCode sessions…")
    con = _connect(path)
    try:
        rows = con.execute("""
            SELECT s.*, p.worktree,
              (SELECT COUNT(*) FROM session_message m
                 WHERE m.session_id=s.id AND m.type IN ('user','assistant')) AS n_messages,
              (SELECT COUNT(*) FROM session_message m
                 WHERE m.session_id=s.id AND m.type='user') AS n_user,
              (SELECT COUNT(*) FROM session_message m
                 WHERE m.session_id=s.id AND m.type='assistant') AS n_assistant,
              (SELECT SUM(LENGTH(m.data)) FROM session_message m
                 WHERE m.session_id=s.id AND m.type IN ('user','assistant')) AS bytes,
              (SELECT m.data FROM session_message m
                 WHERE m.session_id=s.id AND m.type='user'
                 ORDER BY m.seq LIMIT 1) AS first_user
            FROM session_v2 s
            LEFT JOIN project p ON p.id=s.project_id
            ORDER BY s.time_updated DESC
        """).fetchall()
    finally:
        con.close()

    metas: list[SessionMeta] = []
    for r in rows:
        cwd = util.strip_verbatim(str(r["directory"] or r["worktree"] or ""))
        preview = util.one_line(_first_user(r["first_user"]), 120)
        title = util.collapse_ws(str(r["title"] or "")) or preview or r["id"]
        created = util.ts_from_ms(_ms(r["time_created"]))
        updated = util.ts_from_ms(_ms(r["time_updated"]))
        tokens = _ms(r["tokens_input"]) + _ms(r["tokens_output"])
        busy = _ms(r["time_busy"])
        metas.append(SessionMeta(
            source="opencode", id=str(r["id"]), path=str(path),
            project=util.path_to_display(cwd) or "OpenCode",
            cwd=cwd, title=title, n_messages=int(r["n_messages"] or 0),
            n_user=int(r["n_user"] or 0), n_assistant=int(r["n_assistant"] or 0),
            size=int(r["bytes"] or 0), created=created, updated=updated,
            model=_model_name(r["model"]), tokens=tokens or None,
            archived=bool(_ms(r["time_archived"])),
            subagent=bool(r["parent_id"]),
            running=bool(busy and time.time() * 1000 - busy < 300_000),
            preview=preview,
        ))
    return metas


def _tool_output(state: dict) -> str:
    if not isinstance(state, dict):
        return ""
    for key in ("output", "error"):
        val = state.get(key)
        if isinstance(val, str) and val.strip():
            return val
    content = state.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item.get("content"), str):
                    chunks.append(item["content"])
        return "\n".join(c for c in chunks if c)
    meta = state.get("metadata")
    if isinstance(meta, dict):
        for key in ("output", "text", "result"):
            if isinstance(meta.get(key), str):
                return meta[key]
    return ""


def _pretty(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def load_session(meta: SessionMeta) -> LoadedSession:
    con = _connect(Path(meta.path))
    try:
        rows = con.execute("""
            SELECT * FROM session_message
            WHERE session_id=? AND type IN ('user','assistant')
            ORDER BY seq
        """, (meta.id,)).fetchall()
    finally:
        con.close()

    messages: list[Message] = []
    for i, r in enumerate(rows):
        try:
            data = json.loads(r["data"] or "{}")
        except (TypeError, ValueError):
            continue
        role = "user" if r["type"] == "user" else "assistant"
        parts: list[Part] = []
        if role == "user":
            text = data.get("text")
            if isinstance(text, str) and text:
                parts.append(Part(kind="text", text=text))
            content = data.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        parts.append(Part(kind="text", text=b["text"]))
        else:
            content = data.get("content")
            if isinstance(content, str):
                if content:
                    parts.append(Part(kind="text", text=content))
                content = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                typ = block.get("type")
                if typ == "text":
                    parts.append(Part(kind="text", text=block.get("text") or ""))
                elif typ in ("reasoning", "thinking"):
                    parts.append(Part(kind="thinking",
                                      text=block.get("text") or block.get("thinking") or ""))
                elif typ == "tool":
                    state = block.get("state") or {}
                    if not isinstance(state, dict):
                        state = {}
                    name = block.get("name") or state.get("tool") or "tool"
                    args = state.get("input") or state.get("arguments") or {}
                    parts.append(Part(kind="tool_call", name=str(name),
                                      text=_pretty(args),
                                      tool_id=block.get("id") or "",
                                      raw=block))
                    out = _tool_output(state)
                    if out:
                        parts.append(Part(kind="tool_result", name=str(name),
                                          text=out,
                                          tool_id=block.get("id") or "",
                                          raw=block))
                elif typ in ("file", "patch"):
                    text = block.get("text") or block.get("path") or ""
                    if text:
                        parts.append(Part(kind="tool_result",
                                          text=_pretty(text), raw=block))
        if not parts:
            continue
        ts = util.ts_from_ms(_ms((data.get("time") or {}).get("created")
                                 or r["time_created"]))
        messages.append(Message(
            line=i, role=role, ts=ts, parts=parts,
            model=_model_name(data.get("model"))))
    return LoadedSession(meta=meta, messages=messages, raw_lines=[],
                         total_lines=len(messages))
