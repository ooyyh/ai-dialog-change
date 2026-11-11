"""Claude Code session store: ~/.claude/projects/<munged>/<sessionId>.jsonl

Record types observed:
  user / assistant  -> message dicts (content: str | [text|thinking|tool_use|tool_result])
  ai-title          -> {"aiTitle": ...} appended as title changes
  attachment, system, queue-operation, mode, permission-mode,
  file-history-snapshot/delta, last-prompt -> skipped for display
"""
from __future__ import annotations

import json
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

from . import config, util
from .jsonlio import SessionEditor
from .models import LoadedSession, Message, Part, SessionMeta

_DISPLAY_TYPES = {"user", "assistant"}


# ---------------------------------------------------------------- scan

def running_session_ids() -> dict[str, dict]:
    """{sessionId: info} for sessions whose owning pid is still alive."""
    out: dict[str, dict] = {}
    try:
        files = list(config.CLAUDE_SESSIONS.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = o.get("sessionId")
        pid = o.get("pid")
        if sid and util.pid_alive(pid):
            out[sid] = o
    return out


def scan_claude(progress=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    if not config.CLAUDE_PROJECTS.is_dir():
        return metas
    running = running_session_ids()
    dirs = [d for d in config.CLAUDE_PROJECTS.iterdir() if d.is_dir()]
    for n, proj in enumerate(dirs):
        if progress:
            progress(f"claude {n + 1}/{len(dirs)} {proj.name}")
        # top-level <sessionId>.jsonl + nested subagent transcripts
        # (projects/<proj>/<sessionId>/subagents/*.jsonl)
        for f in proj.rglob("*.jsonl"):
            m = _scan_file(f, proj.name)
            if not m:
                continue
            if "subagents" in f.parts:
                m.subagent = True
            else:
                m.running = m.id in running
            metas.append(m)
    return metas


def _scan_file(path: Path, project: str) -> SessionMeta | None:
    try:
        st = path.stat()
        sid = path.stem
        meta = SessionMeta(source="claude", id=sid, path=str(path), project=project,
                           size=st.st_size)
        title = ""
        first_user = ""
        cwd = ""
        model = ""
        n_user = n_asst = 0
        first_ts = last_ts = None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line[0] != "{":
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = o.get("type")
                if t == "ai-title":
                    meta.title = o.get("aiTitle") or meta.title
                    continue
                if t not in _DISPLAY_TYPES:
                    continue
                ts = util.parse_iso(o.get("timestamp"))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if not cwd:
                    cwd = o.get("cwd") or ""
                msg = o.get("message") or {}
                if not model and msg.get("model"):
                    model = msg["model"]
                if o.get("type") == "user" and not o.get("isMeta") and not o.get("isSidechain"):
                    n_user += 1
                    if not first_user:
                        first_user = _user_text(msg)
                elif o.get("type") == "assistant":
                    n_asst += 1
        meta.n_user, meta.n_assistant = n_user, n_asst
        meta.n_messages = n_user + n_asst
        meta.created, meta.updated = first_ts, last_ts or first_ts
        meta.cwd = cwd
        meta.model = model
        meta.preview = util.one_line(first_user, 100)
        if not meta.title:
            meta.title = util.one_line(first_user, 60)
        return meta
    except OSError:
        return None


def _user_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        texts = [p.get("text", "") for p in c
                 if isinstance(p, dict) and p.get("type") == "text"]
        if texts:
            return texts[0]
    return ""


# ---------------------------------------------------------------- load

def load_claude(meta: SessionMeta) -> LoadedSession:
    from .jsonlio import read_lines
    raw = read_lines(meta.path)
    msgs: list[Message] = []
    for i, line in enumerate(raw):
        if not line or line[0] != "{":
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") not in _DISPLAY_TYPES:
            continue
        m = _to_message(i, o)
        if m:
            msgs.append(m)
    lm = LoadedSession(meta=meta, messages=msgs, raw_lines=raw, total_lines=len(raw))
    return lm


def _to_message(i: int, o: dict) -> Message | None:
    typ = o.get("type")
    if o.get("isMeta"):
        return None
    msg = o.get("message") or {}
    content = msg.get("content")
    parts: list[Part] = []
    role = typ or "user"
    if isinstance(content, str):
        if content.strip():
            parts.append(Part(kind="text", text=content))
    elif isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text":
                parts.append(Part(kind="text", text=p.get("text") or ""))
            elif pt == "thinking":
                parts.append(Part(kind="thinking", text=p.get("thinking") or p.get("text") or ""))
            elif pt == "tool_use":
                parts.append(Part(kind="tool_call", name=p.get("name") or "?",
                                  text=_pretty_args(p.get("input")),
                                  tool_id=p.get("id") or "", raw=p))
            elif pt == "tool_result":
                parts.append(Part(kind="tool_result",
                                  text=_result_text(p.get("content")),
                                  tool_id=p.get("tool_use_id") or "", raw=p))
            elif pt == "image":
                parts.append(Part(kind="tool_result", text="[image]", raw=p))
    if not parts:
        return None
    return Message(
        line=i, role=role, ts=util.parse_iso(o.get("timestamp")), parts=parts,
        model=msg.get("model"), uuid=o.get("uuid") or "",
        sidechain=bool(o.get("isSidechain")), claude_record=o,
    )


def _pretty_args(inp) -> str:
    if inp is None:
        return ""
    if isinstance(inp, str):
        return inp
    try:
        return json.dumps(inp, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(inp)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    out.append(p.get("text") or "")
                elif p.get("type") == "image":
                    out.append("[image]")
        return "\n".join(out)
    return str(content or "")


# ---------------------------------------------------------------- edits

def linked_lines(session: LoadedSession, target: Message) -> set[int]:
    """For a claude message, the set of lines to delete together with it:
    an assistant msg carrying tool_use pairs with the user msg holding the
    matching tool_result (and vice versa)."""
    ids = target.tool_ids()
    out = {target.line}
    if not ids:
        return out
    for m in session.messages:
        if m.line == target.line:
            continue
        if m.tool_ids() & ids:
            out.add(m.line)
    return out


def make_editor(session: LoadedSession) -> SessionEditor:
    ed = SessionEditor(session.meta.path)
    ed.note_snapshot("claude", session.meta.id)
    return ed


def apply_text_edits(ed: SessionEditor, msg: Message, new_texts: dict[int, str]) -> None:
    """new_texts: {part index in msg.parts -> new text} for text/thinking parts."""
    rec = json.loads(json.dumps(msg.claude_record))  # deep copy
    m = rec.get("message") or {}
    content = m.get("content")
    part_idx = 0
    if isinstance(content, str):
        if 0 in new_texts:
            m["content"] = new_texts[0]
    elif isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text" and part_idx in new_texts:
                p["text"] = new_texts[part_idx]
            elif pt == "thinking" and part_idx in new_texts:
                p["thinking"] = new_texts[part_idx]
            elif pt == "tool_use" and part_idx in new_texts:
                try:
                    p["input"] = json.loads(new_texts[part_idx])
                except ValueError:
                    p["input"] = new_texts[part_idx]
            elif pt == "tool_result" and part_idx in new_texts:
                p["content"] = new_texts[part_idx]
            part_idx += 1
    ed.replace_line(msg.line, util.dumps_claude(rec))


def apply_raw_edit(ed: SessionEditor, msg: Message, raw_json: str) -> None:
    o = json.loads(raw_json)  # raises on invalid
    ed.replace_line(msg.line, util.dumps_claude(o))


def append_title(ed: SessionEditor, sid: str, title: str) -> None:
    ed.append_line(util.dumps_claude({"type": "ai-title", "aiTitle": title,
                                      "sessionId": sid}))


# ---------------------------------------------------------------- chain repair

def _parse_lines(lines: list[str]) -> list[dict | None]:
    out: list[dict | None] = []
    for ln in lines:
        if not ln or ln[0] != "{":
            out.append(None)
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append(None)
    return out


def repair_after_delete(ed: SessionEditor, deleted: set[int]) -> int:
    """Relink the parentUuid chain (and last-prompt.leafUuid) after deleting
    lines, so the CLI can still rebuild the conversation tree.

    Claude transcripts are a linked list: every record's parentUuid points at
    the previous record's uuid, and last-prompt.leafUuid at the final message.
    A raw line deletion leaves dangling pointers -> the whole session fails to
    render on resume. Call AFTER ed.delete_lines(...), BEFORE ed.save()."""
    objs = _parse_lines(ed.lines)
    deleted_uuids = {o.get("uuid") for i, o in enumerate(objs)
                     if i in deleted and o and o.get("uuid")}
    if not deleted_uuids:
        return 0
    parent_of = {o["uuid"]: o.get("parentUuid") for o in objs
                 if o and o.get("uuid")}

    def ancestor(u: str | None) -> str | None:
        seen: set = set()
        while u in deleted_uuids and u not in seen:
            seen.add(u)
            u = parent_of.get(u)
        return u

    changed = 0
    last_msg_uuid = None
    for i, o in enumerate(objs):
        if not o or i in deleted:
            continue
        pu = o.get("parentUuid")
        if pu in deleted_uuids:
            o["parentUuid"] = ancestor(pu)
            ed.replace_line(i, util.dumps_claude(o))
            changed += 1
        if (o.get("type") in _DISPLAY_TYPES and o.get("uuid")
                and not o.get("isSidechain")):
            last_msg_uuid = o["uuid"]
    # last-prompt records must point at a surviving message
    for i, o in enumerate(objs):
        if not o or i in deleted:
            continue
        if o.get("type") == "last-prompt" and \
                o.get("leafUuid") in deleted_uuids:
            if last_msg_uuid:
                o["leafUuid"] = last_msg_uuid
                ed.replace_line(i, util.dumps_claude(o))
            else:  # no messages left -> the record is meaningless
                ed.delete_line(i)
            changed += 1
    return changed


def dangling_counts(lines: list[str]) -> tuple[int, int]:
    """(# records whose parentUuid targets a missing uuid,
        # last-prompt records whose leafUuid targets a missing uuid)."""
    objs = _parse_lines(lines)
    uuids = {o.get("uuid") for o in objs if o and o.get("uuid")}
    bad_parent = sum(1 for o in objs if o and o.get("parentUuid") is not None
                     and o.get("parentUuid") not in uuids)
    bad_leaf = sum(1 for o in objs if o and o.get("type") == "last-prompt"
                   and o.get("leafUuid") and o.get("leafUuid") not in uuids)
    return bad_parent, bad_leaf


def repair_file_chain(path) -> int:
    """One-shot repair of an already-broken transcript (dangling parentUuid /
    leafUuid). Dangling parents are re-pointed at the nearest preceding record
    of the same sidechain; dangling leafUuid at the last message. Returns the
    number of fixed lines (0 = nothing to fix). The editor's save() backs the
    file up first and writes atomically."""
    ed = SessionEditor(path)
    ed.note_snapshot("claude", Path(path).stem)
    if dangling_counts(ed.lines) == (0, 0):
        return 0
    objs = _parse_lines(ed.lines)
    uuids = {o.get("uuid") for o in objs if o and o.get("uuid")}
    changed = 0
    last_msg_uuid = None
    for i, o in enumerate(objs):
        if not o:
            continue
        if (o.get("type") in _DISPLAY_TYPES and o.get("uuid")
                and not o.get("isSidechain")):
            last_msg_uuid = o["uuid"]
    for i, o in enumerate(objs):
        if not o:
            continue
        pu = o.get("parentUuid")
        if pu is not None and pu not in uuids:
            # nearest preceding record with a uuid (prefer same sidechain)
            new = None
            for j in range(i - 1, -1, -1):
                p = objs[j]
                if p and p.get("uuid"):
                    if bool(p.get("isSidechain")) == bool(o.get("isSidechain")):
                        new = p["uuid"]
                        break
                    if new is None:
                        new = p["uuid"]
            o["parentUuid"] = new
            ed.replace_line(i, util.dumps_claude(o))
            changed += 1
        elif o.get("type") == "last-prompt" and o.get("leafUuid") \
                and o["leafUuid"] not in uuids:
            if last_msg_uuid:
                o["leafUuid"] = last_msg_uuid
                ed.replace_line(i, util.dumps_claude(o))
            else:
                ed.delete_line(i)
            changed += 1
    if changed:
        ed.save()
    return changed


def append_user_message(ed: SessionEditor, sid: str, cwd: str, text: str,
                        version: str = "2.1.211") -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    rec = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd or str(Path.home()),
        "sessionId": sid,
        "version": version,
        "gitBranch": None,
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": str(uuidlib.uuid4()),
        "timestamp": now,
    }
    ed.append_line(util.dumps_claude(rec))
