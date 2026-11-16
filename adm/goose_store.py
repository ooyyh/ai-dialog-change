"""Read-only Goose session store.

Goose stores one JSON file per session:
``~/.local/share/goose/sessions/<id>.json`` on POSIX and
``%APPDATA%\\Block\\goose\\data\\sessions`` on Windows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def sessions_dir() -> Path | None:
    env = os.environ.get("GOOSE_HOME")
    if env:
        base = Path(env).expanduser()
        for cand in (base, base / "sessions"):
            if cand.is_dir() and any(cand.glob("*.json")):
                return cand
        return base / "sessions" if not base.name == "sessions" else base
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA")
                       or Path.home() / "AppData" / "Roaming")
        cands = [
            appdata / "Block" / "goose" / "data" / "sessions",
            appdata / "goose" / "data" / "sessions",
            appdata / "Block" / "goose" / "sessions",
            appdata / "goose" / "sessions",
        ]
    else:
        cands = [
            Path(os.environ.get("XDG_DATA_HOME")
                 or Path.home() / ".local" / "share") / "goose" / "sessions",
        ]
    for p in cands:
        if p.is_dir():
            return p
    return None


def _messages(doc: dict) -> list[dict]:
    conv = doc.get("conversation")
    if isinstance(conv, dict):
        msgs = conv.get("messages")
    elif isinstance(conv, list):
        msgs = conv
    else:
        msgs = doc.get("messages")
    return [m for m in (msgs or []) if isinstance(m, dict)]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for b in content:
            if isinstance(b, str):
                chunks.append(b)
            elif isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    chunks.append(b["text"])
                elif isinstance(b.get("content"), str):
                    chunks.append(b["content"])
        return "\n".join(chunks)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return ""


def _parts(msg: dict) -> list[Part]:
    parts: list[Part] = []
    text = _text(msg.get("content") or msg.get("text"))
    if text:
        parts.append(Part(kind="text", text=text))
    for tool in msg.get("toolRequests") or msg.get("tool_calls") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or tool.get("toolName") or "tool"
        args = tool.get("arguments") or tool.get("input") or {}
        parts.append(Part(kind="tool_call", name=str(name),
                          text=_pretty(args), raw=tool))
        out = tool.get("result") or tool.get("output")
        if out:
            parts.append(Part(kind="tool_result", name=str(name),
                              text=_text(out) or _pretty(out), raw=tool))
    return parts


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
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    msgs = _messages(doc)
    n_user = n_asst = 0
    first_user = ""
    for m in msgs:
        role = str(m.get("role") or "").lower()
        if role == "user":
            n_user += 1
            if not first_user:
                first_user = _text(m.get("content") or m.get("text"))
        elif role in ("assistant", "gemini"):
            n_asst += 1
    title = util.one_line(str(doc.get("description") or ""), 120) \
        or util.one_line(first_user, 80) or path.stem
    cwd = util.strip_verbatim(str(doc.get("working_dir") or ""))
    created = util.parse_iso(doc.get("created_at"))
    updated = util.parse_iso(doc.get("updated_at"))
    if not created and msgs:
        created = util.ts_from_ms(int(msgs[0].get("created") or 0) * 1000)
    if not updated and msgs:
        updated = util.ts_from_ms(int(msgs[-1].get("created") or 0) * 1000)
    return SessionMeta(
        source="goose", id=str(doc.get("id") or path.stem), path=str(path),
        project=util.path_to_display(cwd) or "Goose", cwd=cwd, title=title,
        n_messages=n_user + n_asst, n_user=n_user, n_assistant=n_asst,
        size=st.st_size, created=created, updated=updated or created,
        preview=util.one_line(first_user, 120),
    )


def scan_goose(progress=None, cache=None) -> list[SessionMeta]:
    base = sessions_dir()
    if not base:
        return []
    if progress:
        progress("Goose sessions…")
    files = sorted(base.glob("*.json"))
    metas: list[SessionMeta] = []
    seen: set[str] = set()
    for n, f in enumerate(files):
        try:
            st = f.stat()
        except OSError:
            continue
        seen.add(str(f))
        m = cache.get_meta("goose", str(f), st.st_mtime_ns, st.st_size) \
            if cache is not None else None
        if m is None:
            m = _scan_file(f)
            if m is None:
                continue
            if cache is not None:
                cache.put_meta("goose", str(f), st.st_mtime_ns, st.st_size, m)
        metas.append(m)
        if progress and n % 20 == 0:
            progress(f"Goose {n + 1}/{len(files)}")
    if cache is not None:
        cache.prune_meta("goose", seen)
    return metas


def load_session(meta: SessionMeta) -> LoadedSession:
    try:
        doc = json.loads(Path(meta.path).read_text(encoding="utf-8",
                                                    errors="replace"))
    except (OSError, ValueError):
        doc = {}
    raw = []
    try:
        raw = Path(meta.path).read_text(encoding="utf-8",
                                         errors="replace").splitlines(keepends=True)
    except OSError:
        pass
    messages: list[Message] = []
    for i, m in enumerate(_messages(doc)):
        role = str(m.get("role") or "").lower()
        if role not in ("user", "assistant", "gemini"):
            continue
        parts = _parts(m)
        if not parts:
            continue
        ts = util.ts_from_ms(int(m.get("created") or 0) * 1000)
        messages.append(Message(
            line=i, role="user" if role == "user" else "assistant", ts=ts,
            parts=parts, model=meta.model or None))
    return LoadedSession(meta=meta, messages=messages, raw_lines=raw,
                         total_lines=len(raw))
