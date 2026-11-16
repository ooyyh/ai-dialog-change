"""Read-only Gemini CLI / Qwen Code session store.

Both tools use ``<home>/tmp/<project-hash>/chats/session-*.json``.  The JSON
shape varies slightly between releases, so the reader accepts both the
``type: user|gemini`` records written by current CLIs and role/content pairs
used by older builds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def home(tool: str) -> Path:
    env = os.environ.get("GEMINI_CLI_HOME" if tool == "gemini" else "QWEN_HOME")
    return Path(env).expanduser() if env else Path.home() / f".{tool}"


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


def _role(rec: dict) -> str:
    typ = str(rec.get("type") or rec.get("role") or "").lower()
    if typ in ("user", "human"):
        return "user"
    if typ in ("gemini", "assistant", "model", "ai"):
        return "assistant"
    return ""


def _parts(rec: dict) -> list[Part]:
    parts: list[Part] = []
    text = _text(rec.get("content") or rec.get("text"))
    if text:
        parts.append(Part(kind="text", text=text))
    for tool in rec.get("toolCalls") or rec.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or tool.get("toolName") or "tool"
        args = tool.get("args") or tool.get("input") or {}
        parts.append(Part(kind="tool_call", name=str(name),
                          text=_pretty(args), raw=tool))
        out = tool.get("result") or tool.get("output") or tool.get("response")
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


def _scan_file(path: Path, source: str) -> SessionMeta | None:
    try:
        st = path.stat()
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    sid = str(doc.get("sessionId") or doc.get("id") or path.stem)
    cwd = str(doc.get("cwd") or doc.get("projectRoot") or doc.get("workingDirectory") or "")
    project_hash = str(doc.get("projectHash") or path.parent.parent.name)
    if not cwd:
        for name in (".project_root", "project_root.txt"):
            marker = path.parent.parent / name
            try:
                if marker.is_file():
                    cwd = marker.read_text(encoding="utf-8", errors="replace").strip()
                    break
            except OSError:
                pass
    project = util.path_to_display(cwd) if cwd else project_hash[:8]
    recs = doc.get("messages") or doc.get("history") or []
    n_user = n_asst = 0
    first_user = ""
    model = ""
    created = util.parse_iso(doc.get("startTime"))
    updated = util.parse_iso(doc.get("lastUpdated"))
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        role = _role(rec)
        ts = util.parse_iso(rec.get("timestamp") or rec.get("time"))
        if ts:
            created = created or ts
            updated = ts
        if role == "user":
            n_user += 1
            if not first_user:
                first_user = _text(rec.get("content") or rec.get("text"))
        elif role == "assistant":
            n_asst += 1
            model = str(rec.get("model") or model)
    title = util.one_line(first_user, 80) or sid
    return SessionMeta(
        source=source, id=sid, path=str(path), project=project, cwd=cwd,
        title=title, n_messages=n_user + n_asst, n_user=n_user,
        n_assistant=n_asst, size=st.st_size, created=created,
        updated=updated or created, model=model,
        preview=util.one_line(first_user, 120),
    )


def scan_gemini(tool: str, progress=None, cache=None) -> list[SessionMeta]:
    base = home(tool) / "tmp"
    if not base.is_dir():
        return []
    if progress:
        progress(f"{tool.title()} sessions…")
    files = sorted(base.rglob("chats/session-*.json"))
    metas: list[SessionMeta] = []
    seen: set[str] = set()
    for n, f in enumerate(files):
        try:
            st = f.stat()
        except OSError:
            continue
        seen.add(str(f))
        m = cache.get_meta(tool, str(f), st.st_mtime_ns, st.st_size) \
            if cache is not None else None
        if m is None:
            m = _scan_file(f, tool)
            if m is None:
                continue
            if cache is not None:
                cache.put_meta(tool, str(f), st.st_mtime_ns, st.st_size, m)
        metas.append(m)
        if progress and n % 20 == 0:
            progress(f"{tool.title()} {n + 1}/{len(files)}")
    if cache is not None:
        cache.prune_meta(tool, seen)
    return metas


def load_session(meta: SessionMeta) -> LoadedSession:
    try:
        doc = json.loads(Path(meta.path).read_text(encoding="utf-8",
                                                   errors="replace"))
    except (OSError, ValueError):
        doc = {}
    recs = doc.get("messages") or doc.get("history") or []
    raw = []
    try:
        raw = Path(meta.path).read_text(encoding="utf-8",
                                         errors="replace").splitlines(keepends=True)
    except OSError:
        pass
    messages: list[Message] = []
    for i, rec in enumerate(recs):
        if not isinstance(rec, dict):
            continue
        role = _role(rec)
        if not role:
            continue
        parts = _parts(rec)
        if not parts:
            continue
        messages.append(Message(
            line=i, role=role, ts=util.parse_iso(rec.get("timestamp") or rec.get("time")),
            parts=parts, model=str(rec.get("model") or meta.model or "") or None))
    return LoadedSession(meta=meta, messages=messages, raw_lines=raw,
                         total_lines=len(raw))
