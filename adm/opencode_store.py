"""OpenCode (2.x) session store.

OpenCode keeps sessions in ``opencode.db`` (SQLite).  The desktop/web UI is
backed by the same database, so this works without a running service.  Edits
rewrite ``session_message.data`` (JSON) in place after snapshotting the whole
database into the app backup folder.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from . import config, util
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


def _connect_rw(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:26]


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
        if not isinstance(data, dict):
            continue
        role = "user" if r["type"] == "user" else "assistant"
        parts: list[Part] = []
        if role == "user":
            text = data.get("text")
            if isinstance(text, str) and text:
                parts.append(Part(kind="text", text=text, raw=data))
            content = data.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        parts.append(Part(kind="text", text=b["text"], raw=b))
        else:
            content = data.get("content")
            if isinstance(content, str):
                if content:
                    parts.append(Part(kind="text", text=content, raw=data))
                content = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                typ = block.get("type")
                if typ == "text":
                    parts.append(Part(kind="text", text=block.get("text") or "",
                                      raw=block))
                elif typ in ("reasoning", "thinking"):
                    parts.append(Part(kind="thinking",
                                      text=block.get("text") or block.get("thinking") or "",
                                      raw=block))
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
            model=_model_name(data.get("model")),
            opencode_id=str(r["id"]),
            opencode_record=data))
    return LoadedSession(meta=meta, messages=messages, raw_lines=[],
                         total_lines=len(messages))


# ---------------------------------------------------------------- edits

def _backup(meta: SessionMeta) -> None:
    """Snapshot the session's rows (session + messages) before a mutation.

    The database itself is shared by every OpenCode session and can be
    hundreds of MB, so only the affected rows are stored as JSON."""
    src = Path(meta.path)
    d = config.BACKUP_DIR / "opencode" / meta.id
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    con = _connect(src)
    try:
        session = con.execute("SELECT * FROM session_v2 WHERE id=?",
                              (meta.id,)).fetchone()
        messages = con.execute(
            "SELECT * FROM session_message WHERE session_id=? ORDER BY seq",
            (meta.id,)).fetchall()
    finally:
        con.close()
    payload = {
        "source": "opencode",
        "id": meta.id,
        "path": str(src),
        "taken_at": stamp,
        "session": dict(session) if session else None,
        "messages": [dict(r) for r in messages],
    }
    (d / f"{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        backups = sorted(d.glob("*.json")) + sorted(d.glob("*.db"))
        for old in backups[:max(0, len(backups) - config.BACKUPS_KEEP)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _read_data(meta: SessionMeta, msg: Message) -> dict:
    if not msg.opencode_id:
        raise ValueError("OpenCode message id is missing")
    con = _connect(Path(meta.path))
    try:
        row = con.execute(
            "SELECT data FROM session_message WHERE id=? AND session_id=?",
            (msg.opencode_id, meta.id)).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("OpenCode message no longer exists")
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError) as e:
        raise ValueError("OpenCode message data is not valid JSON") from e
    if not isinstance(data, dict):
        raise ValueError("OpenCode message data is not an object")
    return data


def _write_data(meta: SessionMeta, msg: Message, data: dict) -> None:
    if not msg.opencode_id:
        raise ValueError("OpenCode message id is missing")
    now = _now_ms()
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    con = _connect_rw(Path(meta.path))
    try:
        cur = con.execute(
            "UPDATE session_message SET data=?, time_updated=? "
            "WHERE id=? AND session_id=?",
            (raw, now, msg.opencode_id, meta.id))
        if cur.rowcount == 0:
            raise ValueError("OpenCode message no longer exists")
        con.execute("UPDATE session_v2 SET time_updated=? WHERE id=?",
                    (now, meta.id))
        con.commit()
    finally:
        con.close()


def _targets(data: dict, role: str) -> list[tuple[str, dict | None]]:
    """Editable targets in exactly the same order as load_session() parts."""
    out: list[tuple[str, dict | None]] = []
    if role == "user":
        text = data.get("text")
        if isinstance(text, str) and text:
            out.append(("user_text", None))
        content = data.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    out.append(("block_text", block))
        return out
    content = data.get("content")
    if isinstance(content, str):
        if content:
            out.append(("content_string", None))
        return out
    for block in content or []:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "text":
            out.append(("block_text", block))
        elif typ in ("reasoning", "thinking"):
            out.append(("block_thinking", block))
        elif typ == "tool":
            out.append(("tool_input", block))
            state = block.get("state") or {}
            if isinstance(state, dict) and _tool_output(state):
                out.append(("tool_output", block))
        elif typ in ("file", "patch"):
            out.append(("block_text", block))
    return out


def _apply_text(data: dict, role: str, new_texts: dict[int, str]) -> None:
    targets = _targets(data, role)
    for index, text in new_texts.items():
        if not isinstance(index, int) or index < 0 or index >= len(targets):
            continue
        kind, block = targets[index]
        if kind == "user_text":
            data["text"] = text
        elif kind == "content_string":
            data["content"] = text
        elif kind == "block_text":
            assert block is not None
            block["text"] = text
        elif kind == "block_thinking":
            assert block is not None
            block["text"] = text
        elif kind == "tool_input":
            assert block is not None
            try:
                parsed = json.loads(text)
            except ValueError as e:
                raise ValueError("tool arguments must be valid JSON") from e
            state = block.get("state")
            if not isinstance(state, dict):
                state = {}
                block["state"] = state
            state["input"] = parsed
        elif kind == "tool_output":
            assert block is not None
            state = block.get("state")
            if not isinstance(state, dict):
                state = {}
                block["state"] = state
            if "error" in state and "output" not in state:
                state["error"] = text
            else:
                state["output"] = text


def apply_text_edits(meta: SessionMeta, msg: Message,
                     new_texts: dict[int, str]) -> None:
    """Save edited text/thinking/tool parts for one OpenCode message."""
    if not new_texts:
        return
    _backup(meta)
    data = _read_data(meta, msg)
    _apply_text(data, msg.role, new_texts)
    _write_data(meta, msg, data)


def apply_raw_edit(meta: SessionMeta, msg: Message, raw_json: str) -> None:
    try:
        data = json.loads(raw_json)
    except ValueError as e:
        raise ValueError("invalid JSON") from e
    if not isinstance(data, dict):
        raise ValueError("message data must be a JSON object")
    _backup(meta)
    _write_data(meta, msg, data)


def delete_messages(meta: SessionMeta, msgs: list[Message]) -> int:
    """Delete whole OpenCode message rows and compact their seq numbers."""
    ids = [m.opencode_id for m in msgs if m.opencode_id]
    if not ids:
        return 0
    _backup(meta)
    con = _connect_rw(Path(meta.path))
    try:
        marks = ",".join("?" for _ in ids)
        cur = con.execute(
            f"DELETE FROM session_message WHERE session_id=? AND id IN ({marks})",
            (meta.id, *ids))
        deleted = cur.rowcount
        rows = con.execute(
            "SELECT id FROM session_message WHERE session_id=? ORDER BY seq",
            (meta.id,)).fetchall()
        for seq, row in enumerate(rows, 1):
            con.execute("UPDATE session_message SET seq=? WHERE id=?",
                        (seq, row["id"]))
        con.execute("UPDATE session_v2 SET time_updated=? WHERE id=?",
                    (_now_ms(), meta.id))
        con.commit()
        return deleted
    finally:
        con.close()


def append_user_message(meta: SessionMeta, text: str) -> str:
    """Append a human user message so the OpenCode session can be resumed."""
    _backup(meta)
    now = _now_ms()
    msg_id = _new_id("msg_")
    data = {"time": {"created": now}, "text": text,
            "files": [], "agents": []}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    con = _connect_rw(Path(meta.path))
    try:
        row = con.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq "
            "FROM session_message WHERE session_id=?", (meta.id,)).fetchone()
        seq = int(row["seq"] if row else 1)
        con.execute(
            "INSERT INTO session_message "
            "(id, session_id, type, seq, time_created, time_updated, data) "
            "VALUES (?, ?, 'user', ?, ?, ?, ?)",
            (msg_id, meta.id, seq, now, now, raw))
        con.execute("UPDATE session_v2 SET time_updated=? WHERE id=?",
                    (now, meta.id))
        con.commit()
        return msg_id
    finally:
        con.close()


def set_archived(meta: SessionMeta, archived: bool) -> None:
    now = _now_ms()
    con = _connect_rw(Path(meta.path))
    try:
        con.execute("UPDATE session_v2 SET time_archived=?, time_updated=? "
                    "WHERE id=?",
                    (now if archived else None, now, meta.id))
        con.commit()
    finally:
        con.close()


def rename_session(meta: SessionMeta, title: str) -> None:
    con = _connect_rw(Path(meta.path))
    try:
        cur = con.execute("UPDATE session_v2 SET title=?, time_updated=? "
                          "WHERE id=?", (title, _now_ms(), meta.id))
        if cur.rowcount == 0:
            raise ValueError("OpenCode session no longer exists")
        con.commit()
    finally:
        con.close()


def delete_session(meta: SessionMeta) -> None:
    """Delete one session row; messages go with it (ON DELETE CASCADE)."""
    _backup(meta)
    con = _connect_rw(Path(meta.path))
    try:
        con.execute("DELETE FROM session_message WHERE session_id=?", (meta.id,))
        con.execute("DELETE FROM session_v2 WHERE id=?", (meta.id,))
        con.commit()
    finally:
        con.close()


def duplicate_session(meta: SessionMeta) -> str:
    """Copy an OpenCode session (metadata + messages) under a fresh id."""
    _backup(meta)
    new_id = _new_id("ses_")
    now = _now_ms()
    con = _connect_rw(Path(meta.path))
    try:
        row = con.execute("SELECT * FROM session_v2 WHERE id=?",
                          (meta.id,)).fetchone()
        if not row:
            raise ValueError("OpenCode session no longer exists")
        data = dict(row)
        data.update({
            "id": new_id,
            "title": (str(data.get("title") or "Copy")) + " (copy)",
            "slug": str(data.get("slug") or "copy") + "-copy",
            "parent_id": None,
            "fork_session_id": meta.id,
            "share_url": None,
            "revert": None,
            "time_archived": None,
            "time_busy": None,
            "time_idle": None,
            "time_created": now,
            "time_updated": now,
        })
        cols = list(data)
        con.execute(
            f"INSERT INTO session_v2 ({','.join(cols)}) "
            f"VALUES ({','.join('?' for _ in cols)})",
            [data[c] for c in cols])
        messages = con.execute(
            "SELECT * FROM session_message WHERE session_id=? ORDER BY seq",
            (meta.id,)).fetchall()
        for msg in messages:
            mdata = dict(msg)
            mdata["id"] = _new_id("msg_")
            mdata["session_id"] = new_id
            mdata["time_created"] = now
            mdata["time_updated"] = now
            mcols = list(mdata)
            con.execute(
                f"INSERT INTO session_message ({','.join(mcols)}) "
                f"VALUES ({','.join('?' for _ in mcols)})",
                [mdata[c] for c in mcols])
        con.commit()
        return new_id
    finally:
        con.close()
