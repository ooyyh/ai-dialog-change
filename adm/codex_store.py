"""Codex session store.

List view reads ~/.codex/state_5.sqlite `threads` (title / tokens / archived /
pinned / cwd) with a rollout-file-walk fallback. Message stream parses the
rollout .jsonl: records are {timestamp, type, payload} with type in
{session_meta, event_msg, response_item, world_state, turn_context}.
response_item payloads: message / reasoning / function_call /
function_call_output (plus generic *_call / *_output variants).

Editing notes: the rollout file is the source of truth, but Codex caches a
projection in thread_history_1.sqlite with byte offsets — after editing a
rollout we delete that thread's cached rows so Codex re-projects from disk.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as _uriquote

from . import config, util
from .jsonlio import SessionEditor
from .models import LoadedSession, Message, Part, SessionMeta

# user input_text blocks that are harness injections, not human input
_INJECT_PREFIXES = (
    "<environment_context", "<skills_instructions", "<permissions instructions",
    "<collaboration_mode", "<turn_aborted", "<skill>", "<user_instructions",
    "<app_instructions", "<agenda_context", "<system", "<remote",
)


def _connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{_uriquote(str(path))}" + ("?mode=ro" if readonly else "?mode=rw")
    con = sqlite3.connect(uri, uri=True, timeout=4)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- scan

def scan_codex(progress=None, cache=None) -> list[SessionMeta]:
    try:
        return _scan_sqlite(progress, cache=cache)
    except sqlite3.Error:
        return _scan_files(progress, cache=cache)


_count_cache: dict[str, tuple[int, int, int]] = {}  # path -> (mtime_ns, size, count)


def _quick_msg_count(path: str, cache=None) -> int:
    """Byte-level user+assistant message count; cached by mtime+size.
    Slightly over-counts (includes harness-injected user blocks) — fine for
    a list column, and avoids JSON-parsing 80MB rollouts on every scan."""
    try:
        st = Path(path).stat()
    except OSError:
        return 0
    hit = _count_cache.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    if cache is not None:
        saved = cache.get_value("codex_count", path, st.st_mtime_ns, st.st_size)
        if isinstance(saved, int):
            _count_cache[path] = (st.st_mtime_ns, st.st_size, saved)
            return saved
    n = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                n += line.count(b'"role":"user"') + line.count(b'"role":"assistant"')
                n += line.count(b'"role": "user"') + line.count(b'"role": "assistant"')
    except OSError:
        return 0
    _count_cache[path] = (st.st_mtime_ns, st.st_size, n)
    if cache is not None:
        cache.put_value("codex_count", path, st.st_mtime_ns, st.st_size, n)
    return n


def _scan_sqlite(progress=None, cache=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    if progress:
        progress("codex sqlite threads")
    con = _connect(config.CODEX_STATE_DB)
    now = time.time()
    lock_ids = _locked_thread_ids()
    rows = con.execute("SELECT * FROM threads").fetchall()
    seen: set[str] = set()
    for k, r in enumerate(rows):
        if progress and k % 10 == 0:
            progress(f"codex counting {k + 1}/{len(rows)}")
        p = r["rollout_path"]
        if not p or not Path(p).exists():
            continue
        seen.add(str(p))
        cwd = util.strip_verbatim(r["cwd"] or "")
        created = util.ts_from_ms(r["created_at_ms"] or (r["created_at"] or 0) * 1000)
        updated = util.ts_from_ms(r["updated_at_ms"] or (r["updated_at"] or 0) * 1000)
        m = SessionMeta(
            source="codex", id=r["id"], path=p,
            project=util.path_to_display(cwd or str(Path(p).parent)),
            cwd=cwd, title=util.collapse_ws(r["title"] or ""), size=Path(p).stat().st_size,
            created=created, updated=updated,
            model=r["model"] or "", tokens=r["tokens_used"],
            archived=bool(r["archived"]),
            n_messages=_quick_msg_count(p, cache=cache),
            preview=util.one_line(util.collapse_ws(r["first_user_message"] or ""), 100),
        )
        m.running = r["id"] in lock_ids or _recently_touched(p, 180)
        metas.append(m)
    con.close()
    if cache is not None:
        cache.prune_values("codex_count", seen)
    return metas


def _recently_touched(path: str, seconds: float) -> bool:
    try:
        return time.time() - Path(path).stat().st_mtime < seconds
    except OSError:
        return False


def _locked_thread_ids() -> set[str]:
    out: set[str] = set()
    try:
        for f in config.CODEX_LOCKS.iterdir():
            name = f.name
            if name.endswith(".lock") or name.endswith(".json"):
                out.add(name.rsplit(".", 1)[0])
            else:
                out.add(name)
    except OSError:
        pass
    return out


def _scan_files(progress=None, cache=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    files = sorted(config.CODEX_SESSIONS.rglob("rollout-*.jsonl"))
    seen: set[str] = set()
    for n, f in enumerate(files):
        if progress and n % 10 == 0:
            progress(f"codex files {n + 1}/{len(files)}")
        seen.add(str(f))
        m = _scan_rollout(f)
        if m:
            m.running = _recently_touched(str(f), 180)
            metas.append(m)
    if cache is not None:
        cache.prune_values("codex_count", seen)
    return metas


def _scan_rollout(f: Path) -> SessionMeta | None:
    try:
        meta = SessionMeta(source="codex", id=f.stem.split("-", 5)[-1]
                           if f.stem.count("-") >= 5 else f.stem,
                           path=str(f), project=util.path_to_display(str(f.parent)),
                           size=f.stat().st_size)
        n_user = n_asst = 0
        first_user = ""
        first_ts = last_ts = None
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"session_meta"' in line:
                    try:
                        o = json.loads(line)
                        p = o.get("payload") or {}
                        meta.id = p.get("session_id") or meta.id
                        meta.cwd = util.strip_verbatim(p.get("cwd") or "")
                        meta.project = util.path_to_display(meta.cwd or str(f.parent))
                        meta.model = p.get("model_provider") or ""
                    except ValueError:
                        pass
                    continue
                if '"response_item"' not in line:
                    continue
                if '"message"' not in line and '"function_call"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                ts = util.parse_iso(o.get("timestamp"))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                p = o.get("payload") or {}
                if p.get("type") != "message":
                    continue
                role = p.get("role")
                text = _payload_text(p)
                if role == "user":
                    if not text or text.startswith(_INJECT_PREFIXES):
                        continue
                    n_user += 1
                    if not first_user:
                        first_user = text
                elif role == "assistant":
                    n_asst += 1
        meta.n_user, meta.n_assistant = n_user, n_asst
        meta.n_messages = n_user + n_asst
        meta.created, meta.updated = first_ts, last_ts or first_ts
        meta.preview = util.one_line(first_user, 100)
        meta.title = util.one_line(first_user, 60)
        return meta
    except OSError:
        return None


# ---------------------------------------------------------------- load

def load_codex(meta: SessionMeta) -> LoadedSession:
    from .jsonlio import read_lines
    raw = read_lines(meta.path)
    msgs: list[Message] = []
    has_response_items = False
    for i, line in enumerate(raw):
        if '"response_item"' not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        m = _to_message(i, o)
        if m:
            has_response_items = True
            msgs.append(m)
    if not has_response_items:
        # legacy sessions store the conversation as event_msg records only
        for i, line in enumerate(raw):
            if '"event_msg"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = _to_message(i, o)
            if m:
                msgs.append(m)
    return LoadedSession(meta=meta, messages=msgs, raw_lines=raw, total_lines=len(raw))


def _payload_text(payload: dict) -> str:
    out = []
    for p in payload.get("content") or []:
        if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text"):
            out.append(p.get("text") or "")
    return "\n".join(out)


def _to_message(i: int, o: dict) -> Message | None:
    typ = o.get("type")
    ts = util.parse_iso(o.get("timestamp"))
    if typ == "response_item":
        p = o.get("payload") or {}
        pt = p.get("type") or ""
        if pt == "message":
            role = p.get("role")
            if role == "developer":
                return None
            text = _payload_text(p)
            if role == "user" and (not text or text.startswith(_INJECT_PREFIXES)):
                return None
            if not text.strip():
                return None
            return Message(line=i, role=role or "user", ts=ts,
                           parts=[Part(kind="text", text=text)],
                           codex_payload=p)
        if pt == "reasoning":
            summ = "\n".join(s.get("text", "") for s in (p.get("summary") or [])
                             if isinstance(s, dict))
            return Message(line=i, role="reasoning", ts=ts,
                           parts=[Part(kind="reasoning", text=summ or "(encrypted reasoning)")],
                           codex_payload=p)
        if pt.endswith("_output") or pt == "function_call_output":
            out = p.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False) if out is not None else ""
            return Message(line=i, role="tool_result", ts=ts,
                           parts=[Part(kind="tool_result", text=out,
                                       tool_id=p.get("call_id") or "")],
                           codex_payload=p)
        if pt.endswith("_call") and pt != "function_call_output":
            args = p.get("arguments")
            if isinstance(args, str):
                pretty = args
            else:
                pretty = json.dumps(args, ensure_ascii=False, indent=2) if args else ""
            return Message(line=i, role="tool_call", ts=ts,
                           parts=[Part(kind="tool_call", name=p.get("name") or pt,
                                       text=pretty, tool_id=p.get("call_id") or "")],
                           codex_payload=p)
        return None
    if typ == "event_msg":
        p = o.get("payload") or {}
        et = p.get("type")
        if et == "user_message":
            text = p.get("message") or ""
            if text and not text.startswith(_INJECT_PREFIXES):
                return Message(line=i, role="user", ts=ts,
                               parts=[Part(kind="text", text=text)])
        if et == "agent_message":
            text = p.get("message") or ""
            if text:
                return Message(line=i, role="assistant", ts=ts,
                               parts=[Part(kind="text", text=text)])
    return None


# ---------------------------------------------------------------- edits

def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    ids = target.tool_ids()
    out = {target.line}
    if not ids:
        return out
    for m in session.messages:
        if m.line != target.line and (m.tool_ids() & ids):
            out.add(m.line)
    return out


def make_editor(session: LoadedSession) -> SessionEditor:
    ed = SessionEditor(session.meta.path)
    ed.note_snapshot("codex", session.meta.id)
    return ed


def apply_text_edits(ed: SessionEditor, msg: Message, new_texts: dict[int, str]) -> None:
    p = json.loads(json.dumps(msg.codex_payload))
    if p.get("type") == "message":
        idx = 0
        for part in p.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"):
                if idx in new_texts:
                    part["text"] = new_texts[idx]
                idx += 1
    elif p.get("type") == "function_call":
        if 0 in new_texts:
            p["arguments"] = new_texts[0]  # keep as string; validated as JSON in dialog
    elif p.get("type") == "function_call_output":
        if 0 in new_texts:
            p["output"] = new_texts[0]
    ed.replace_line(msg.line, util.dumps_codex(
        _wrap_payload(msg, p)))


def apply_raw_edit(ed: SessionEditor, msg: Message, raw_json: str) -> None:
    o = json.loads(raw_json)
    ed.replace_line(msg.line, util.dumps_codex(o))


def _wrap_payload(msg: Message, payload: dict) -> dict:
    """Rebuild the full record {timestamp, type, payload} around an edited payload."""
    src = msg.codex_payload
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rec = {"timestamp": ts, "type": "response_item", "payload": payload}
    passthrough = (src or {}).get("internal_chat_message_metadata_passthrough")
    if passthrough:
        payload.setdefault("internal_chat_message_metadata_passthrough", passthrough)
    return rec


def append_user_message(ed: SessionEditor, text: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rec = {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }
    ed.append_line(util.dumps_codex(rec))


# ------------------------------------------------ sqlite-side effects

def rename_thread(sid: str, title: str) -> None:
    with _connect(config.CODEX_STATE_DB, readonly=False) as con:
        con.execute("UPDATE threads SET title=?, preview=substr(?,1,200) WHERE id=?",
                    (title, title, sid))


def set_archived(sid: str, archived: bool) -> None:
    ts = int(time.time() * 1000)
    with _connect(config.CODEX_STATE_DB, readonly=False) as con:
        con.execute("UPDATE threads SET archived=?, archived_at=? WHERE id=?",
                    (1 if archived else 0, ts if archived else None, sid))


def set_pinned(sid: str, pinned: bool) -> None:
    with _connect(config.CODEX_STATE_DB, readonly=False) as con:
        con.execute("UPDATE threads SET is_pinned=? WHERE id=?", (1 if pinned else 0, sid))


def invalidate_history(sid: str) -> None:
    """Drop the sqlite projection for this thread so Codex re-reads the rollout.
    Call after editing a rollout file. Best effort."""
    try:
        with _connect(config.CODEX_THREAD_HISTORY_DB, readonly=False) as con:
            con.execute("DELETE FROM thread_items WHERE thread_id=?", (sid,))
            con.execute("DELETE FROM thread_turns WHERE thread_id=?", (sid,))
            con.execute("DELETE FROM thread_history_projection_state WHERE thread_id=?", (sid,))
    except sqlite3.Error:
        pass


def _uuid7() -> str:
    """Codex session ids are time-ordered uuid7s — generate a matching one."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    b = bytearray(uuidlib.uuid4().bytes)
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuidlib.UUID(bytes=bytes(b)))


def new_session_id() -> str:
    """Fresh time-ordered id for a transplanted Codex thread."""
    return _uuid7()


def duplicate_session(meta: SessionMeta, dest_dir: Path | None = None) -> str | None:
    """Copy a rollout under a fresh session id and register it in sqlite."""
    new_id = _uuid7()
    src = Path(meta.path)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    d = dest_dir or src.parent
    dst = d / f"rollout-{ts}-{new_id}.jsonl"
    lines = []
    for line in src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
        if '"session_meta"' in line:
            try:
                o = json.loads(line)
                (o.get("payload") or {}).update({"session_id": new_id, "id": new_id})
                line = util.dumps_codex(o) + "\n"
            except ValueError:
                pass
        lines.append(line)
    dst.write_text("".join(lines), encoding="utf-8")
    try:
        with _connect(config.CODEX_STATE_DB, readonly=False) as con:
            row = con.execute("SELECT * FROM threads WHERE id=?", (meta.id,)).fetchone()
            if row is not None:
                cols = [c[0] for c in con.execute("PRAGMA table_info(threads)")]
                vals = dict(row)
                vals["id"] = new_id
                vals["rollout_path"] = str(dst)
                vals["archived"] = 0
                vals["created_at"] = vals["updated_at"] = int(time.time())
                vals["created_at_ms"] = vals["updated_at_ms"] = int(time.time() * 1000)
                q = (f"INSERT INTO threads ({','.join(cols)}) "
                     f"VALUES ({','.join('?' * len(cols))})")
                con.execute(q, [vals.get(c) for c in cols])
        return new_id
    except sqlite3.Error:
        return new_id  # file copied; sqlite registration failed but usable later
