"""Read-only Qoder desktop session store.

Qoder keeps chat history in ``main.sqlite`` (``chat_sessions`` +
``chat_session_messages``).  Only the user/assistant text projection is
consumed here; Qoder remains the owner of its database.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def db_path() -> Path | None:
    env = os.environ.get("QODER_HOME") or os.environ.get("QODER_DATA_DIR")
    if env:
        base = Path(env).expanduser()
        for cand in (base / "main.sqlite", base):
            if cand.is_file():
                return cand
    appdata = Path(os.environ.get("APPDATA")
                   or Path.home() / "AppData" / "Roaming")
    p = appdata / "com.qodercn.app.stable" / "main.sqlite"
    return p if p.is_file() else None


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=4)
    con.row_factory = sqlite3.Row
    return con


def _ms(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _payload(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _first_text(payload: dict) -> str:
    text = payload.get("text")
    if isinstance(text, str):
        return text
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                chunks.append(b["text"])
        return "\n".join(chunks)
    return ""


def scan_qoder(progress=None, cache=None) -> list[SessionMeta]:
    path = db_path()
    if not path:
        return []
    if progress:
        progress("Qoder sessions…")
    con = _connect(path)
    try:
        rows = con.execute("""
            SELECT s.*,
              (SELECT COUNT(*) FROM chat_session_messages m
                 WHERE m.session_id=s.session_id) AS n_messages,
              (SELECT COUNT(*) FROM chat_session_messages m
                 WHERE m.session_id=s.session_id
                   AND json_extract(m.payload_json, '$.role')='user') AS n_user,
              (SELECT COUNT(*) FROM chat_session_messages m
                 WHERE m.session_id=s.session_id
                   AND json_extract(m.payload_json, '$.role')='assistant') AS n_assistant,
              (SELECT SUM(LENGTH(m.payload_json)) FROM chat_session_messages m
                 WHERE m.session_id=s.session_id) AS bytes,
              (SELECT m.payload_json FROM chat_session_messages m
                 WHERE m.session_id=s.session_id
                 ORDER BY m.sequence LIMIT 1) AS first_payload
            FROM chat_sessions s
            WHERE s.deleted_at IS NULL
            ORDER BY s.updated_at DESC
        """).fetchall()
    finally:
        con.close()

    metas: list[SessionMeta] = []
    for r in rows:
        payload = _payload(r["first_payload"])
        preview = util.one_line(_first_text(payload), 120)
        title = util.collapse_ws(str(r["title"] or "")) or preview or r["session_id"]
        cwd = util.strip_verbatim(str(r["cwd"] or ""))
        metas.append(SessionMeta(
            source="qoder", id=str(r["session_id"]), path=str(path),
            project=util.path_to_display(cwd) or "Qoder", cwd=cwd,
            title=title, n_messages=int(r["n_messages"] or 0),
            n_user=int(r["n_user"] or 0), n_assistant=int(r["n_assistant"] or 0),
            size=int(r["bytes"] or 0),
            created=util.ts_from_ms(_ms(r["created_at"])),
            updated=util.ts_from_ms(_ms(r["updated_at"])),
            model=str(r["model"] or ""), archived=bool(r["archived"]),
            preview=preview,
        ))
    return metas


def _tool_text(tool) -> str:
    if isinstance(tool, str):
        return tool
    try:
        return json.dumps(tool, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(tool)


def load_session(meta: SessionMeta) -> LoadedSession:
    con = _connect(Path(meta.path))
    try:
        rows = con.execute("""
            SELECT * FROM chat_session_messages
            WHERE session_id=? ORDER BY sequence
        """, (meta.id,)).fetchall()
    finally:
        con.close()

    messages: list[Message] = []
    for i, r in enumerate(rows):
        data = _payload(r["payload_json"])
        role = data.get("role") or "user"
        if role not in ("user", "assistant"):
            continue
        parts: list[Part] = []
        text = _first_text(data)
        if text:
            parts.append(Part(kind="thinking" if role == "assistant" and
                              data.get("thinking") else "text", text=text))
        for tool in data.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or tool.get("toolName") or "tool"
            status = tool.get("status") or ""
            args = tool.get("input") or tool.get("arguments") or {}
            parts.append(Part(kind="tool_call", name=str(name),
                              text=_tool_text(args), raw=tool))
            out = tool.get("output") or tool.get("result") or tool.get("error")
            if out:
                parts.append(Part(kind="tool_result", name=str(name),
                                  text=_tool_text(out), raw=tool))
            elif status:
                parts.append(Part(kind="tool_result", name=str(name),
                                  text=str(status), raw=tool))
        if not parts:
            continue
        ts = util.parse_iso(data.get("timestamp"))
        messages.append(Message(line=i, role=role, ts=ts, parts=parts,
                                model=meta.model or None))
    return LoadedSession(meta=meta, messages=messages, raw_lines=[],
                         total_lines=len(messages))
