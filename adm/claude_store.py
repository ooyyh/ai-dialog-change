"""Claude Code session store: ~/.claude/projects/<munged>/<sessionId>.jsonl

Record types observed:
  user / assistant  -> message dicts (content: str | [text|thinking|tool_use|tool_result])
  ai-title          -> {"aiTitle": ...} appended as title changes
  attachment, system, queue-operation, mode, permission-mode,
  file-history-snapshot/delta, last-prompt -> skipped for display
"""
from __future__ import annotations

import json
import os
import re
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import config, util
from .jsonlio import SessionEditor
from .models import LoadedSession, Message, Part, SessionMeta

_DISPLAY_TYPES = {"user", "assistant"}
_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
_CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]*)"')
_USER_RE = re.compile(rb'"type"\s*:\s*"user"')
_ASST_RE = re.compile(rb'"type"\s*:\s*"assistant"')
_TITLE_RE = re.compile(rb'"type"\s*:\s*"ai-title"')


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


def scan_claude(progress=None, cache=None) -> list[SessionMeta]:
    metas: list[SessionMeta] = []
    if not config.CLAUDE_PROJECTS.is_dir():
        return metas
    running = running_session_ids()
    dirs = sorted(d for d in config.CLAUDE_PROJECTS.iterdir() if d.is_dir())
    jobs: list[tuple[Path, str, int, int]] = []
    seen: set[str] = set()
    for n, proj in enumerate(dirs):
        if progress:
            progress(f"claude {n + 1}/{len(dirs)} {proj.name}")
        # top-level <sessionId>.jsonl + nested subagent transcripts
        # (projects/<proj>/<sessionId>/subagents/*.jsonl)
        for f in sorted(proj.rglob("*.jsonl")):
            try:
                st = f.stat()
            except OSError:
                continue
            p = str(f)
            seen.add(p)
            m = cache.get_meta("claude", p, st.st_mtime_ns, st.st_size) \
                if cache is not None else None
            if m is not None:
                m.path = p
                m.project = proj.name
                m.size = st.st_size
            else:
                jobs.append((f, proj.name, st.st_mtime_ns, st.st_size))
                continue
            if "subagents" in f.parts:
                m.subagent = True
                m.running = False
            else:
                m.subagent = False
                m.running = m.id in running
            metas.append(m)

    if jobs:
        if progress:
            progress(f"claude parsing {len(jobs)} changed file(s)")
        workers = min(8, max(1, os.cpu_count() or 4), len(jobs))
        if workers <= 1:
            results = ((job, _scan_file(job[0], job[1], _Stat(job[2], job[3])))
                       for job in jobs)
            for k, item in enumerate(results):
                _accept_claude(item, proj_running=running, cache=cache, metas=metas)
                if progress and k % 20 == 0:
                    progress(f"claude parsing {k + 1}/{len(jobs)}")
        else:
            def work(job):
                try:
                    return job, _scan_file(job[0], job[1],
                                           _Stat(job[2], job[3]))
                except Exception:  # noqa: BLE001 — one bad file must not stop scan
                    return job, None

            with ThreadPoolExecutor(max_workers=workers) as ex:
                for k, item in enumerate(ex.map(work, jobs)):
                    _accept_claude(item, proj_running=running, cache=cache,
                                   metas=metas)
                    if progress and k % 20 == 0:
                        progress(f"claude parsing {k + 1}/{len(jobs)}")
    if cache is not None:
        cache.prune_meta("claude", seen)
    return metas


class _Stat:
    """Tiny stand-in for os.stat_result used by _scan_file."""
    __slots__ = ("st_mtime_ns", "st_size")

    def __init__(self, mtime_ns: int, size: int):
        self.st_mtime_ns = mtime_ns
        self.st_size = size


def _accept_claude(item, *, proj_running: dict, cache, metas: list) -> None:
    job, m = item
    if m is None:
        return
    f, project, mtime_ns, size = job
    m.subagent = "subagents" in f.parts
    m.running = (not m.subagent) and m.id in proj_running
    if cache is not None:
        cache.put_meta("claude", str(f), mtime_ns, size, m)
    metas.append(m)


def _unescape_json_bytes(raw: bytes) -> str:
    try:
        return json.loads('"' + raw.decode("utf-8", "replace") + '"')
    except ValueError:
        return raw.decode("utf-8", "replace")


def _scan_file(path: Path, project: str, st=None) -> SessionMeta | None:
    try:
        if st is None:
            st = path.stat()
        sid = path.stem
        meta = SessionMeta(source="claude", id=sid, path=str(path), project=project,
                           size=st.st_size)
        first_user = ""
        cwd = ""
        model = ""
        n_user = n_asst = 0
        first_ts = last_ts = None
        with open(path, "rb") as f:
            for raw in f:
                is_user = _USER_RE.search(raw) is not None
                is_asst = _ASST_RE.search(raw) is not None
                is_title = _TITLE_RE.search(raw) is not None
                if not (is_user or is_asst or is_title):
                    continue
                ts_m = _TS_RE.search(raw)
                if ts_m:
                    ts = util.parse_iso(ts_m.group(1).decode("utf-8", "replace"))
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                if is_title:
                    try:
                        o = json.loads(raw.decode("utf-8", "replace"))
                        meta.title = o.get("aiTitle") or meta.title
                    except ValueError:
                        pass
                    continue
                if is_user:
                    try:
                        o = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if o.get("isMeta") or o.get("isSidechain"):
                        continue
                    n_user += 1
                    if not first_user:
                        first_user = _user_text(o.get("message") or {})
                elif is_asst:
                    n_asst += 1
                if not cwd:
                    m = _CWD_RE.search(raw)
                    if m:
                        cwd = _unescape_json_bytes(m.group(1))
                if not model:
                    m = _MODEL_RE.search(raw)
                    if m:
                        model = m.group(1).decode("utf-8", "replace")
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
