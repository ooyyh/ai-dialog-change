"""Read-only Cline / Roo Code task store (VS Code globalStorage).

Each task is a folder under::

    <globalStorage>/<extension>/tasks/<task-id>/
        api_conversation_history.json
        ui_messages.json
        task_metadata.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def global_storage_roots() -> list[Path]:
    env = os.environ.get("ADM_VSCODE_GLOBAL_STORAGE")
    if env:
        return [Path(env).expanduser()]
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA")
                       or Path.home() / "AppData" / "Roaming")
        names = ("Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium")
        return [appdata / n / "User" / "globalStorage" for n in names]
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        names = ("Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium")
        return [base / n / "User" / "globalStorage" for n in names]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    names = ("Code", "Code - Insiders", "Cursor", "Windsurf", "VSCodium")
    return [base / n / "User" / "globalStorage" for n in names]


def _ext(tool: str) -> str:
    return ("saoudrizwan.claude-dev" if tool == "cline"
            else "rooveterinaryinc.roo-cline")


def task_roots(tool: str) -> list[Path]:
    out = []
    for root in global_storage_roots():
        p = root / _ext(tool) / "tasks"
        if p.is_dir():
            out.append(p)
    return out


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return default


def _content_text(content) -> str:
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


def _history(task: Path) -> list[dict]:
    data = _read_json(task / "api_conversation_history.json", [])
    return [m for m in (data or []) if isinstance(m, dict)]


def _ui(task: Path) -> list[dict]:
    data = _read_json(task / "ui_messages.json", [])
    return [m for m in (data or []) if isinstance(m, dict)]


def _fp(task: Path) -> tuple[int, int]:
    mtime = 0
    size = 0
    for name in ("api_conversation_history.json", "ui_messages.json",
                 "task_metadata.json"):
        p = task / name
        try:
            st = p.stat()
        except OSError:
            continue
        mtime = max(mtime, st.st_mtime_ns)
        size += st.st_size
    return mtime, size


def _scan_task(task: Path, tool: str) -> SessionMeta | None:
    hist = _history(task)
    if not hist:
        return None
    n_user = n_asst = 0
    first_user = ""
    created = updated = None
    for m in hist:
        role = str(m.get("role") or "")
        ts = util.ts_from_ms(m.get("ts"))
        if ts:
            created = created or ts
            updated = ts
        if role == "user":
            n_user += 1
            if not first_user:
                first_user = _content_text(m.get("content"))
        elif role == "assistant":
            n_asst += 1
    meta = _read_json(task / "task_metadata.json", {})
    if not isinstance(meta, dict):
        meta = {}
    title = util.one_line(str(meta.get("title") or ""), 100) \
        or util.one_line(first_user, 80) or task.name
    model = ""
    usage = meta.get("model_usage") or []
    if usage and isinstance(usage[-1], dict):
        model = str(usage[-1].get("model_id") or usage[-1].get("model") or "")
    cwd = str(meta.get("cwd") or "")
    if not cwd:
        for env in reversed(meta.get("environment_history") or []):
            if isinstance(env, dict) and env.get("cwd"):
                cwd = str(env["cwd"])
                break
    fp_mtime, fp_size = _fp(task)
    return SessionMeta(
        source=tool, id=task.name, path=str(task),
        project=util.path_to_display(cwd) or
        ("Cline" if tool == "cline" else "Roo Code"),
        cwd=cwd, title=title,
        n_messages=n_user + n_asst, n_user=n_user, n_assistant=n_asst,
        size=fp_size, created=created, updated=updated, model=model,
        preview=util.one_line(first_user, 120),
    )


def scan_cline(tool: str, progress=None, cache=None) -> list[SessionMeta]:
    bases = task_roots(tool)
    if not bases:
        return []
    if progress:
        progress(("Cline" if tool == "cline" else "Roo Code") + " tasks…")
    metas: list[SessionMeta] = []
    seen: set[str] = set()
    for base in bases:
        for task in sorted(p for p in base.iterdir() if p.is_dir()):
            key = str(task)
            seen.add(key)
            fp_mtime, fp_size = _fp(task)
            m = cache.get_meta(tool, key, fp_mtime, fp_size) \
                if cache is not None else None
            if m is None:
                m = _scan_task(task, tool)
                if m is None:
                    continue
                if cache is not None:
                    cache.put_meta(tool, key, fp_mtime, fp_size, m)
            metas.append(m)
    if cache is not None:
        cache.prune_meta(tool, seen)
    return metas


def _parts(m: dict) -> list[Part]:
    parts: list[Part] = []
    content = m.get("content")
    text = _content_text(content)
    if text:
        parts.append(Part(kind="text", text=text))
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            typ = b.get("type")
            if typ in ("tool_use", "tool_call"):
                name = b.get("name") or "tool"
                parts.append(Part(kind="tool_call", name=str(name),
                                  text=_pretty(b.get("input") or b.get("arguments")),
                                  tool_id=b.get("id") or "", raw=b))
            elif typ in ("tool_result", "tool_response"):
                parts.append(Part(kind="tool_result",
                                  text=_content_text(b.get("content") or b.get("output")),
                                  tool_id=b.get("tool_use_id") or "", raw=b))
    return parts


def _pretty(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def load_session(meta: SessionMeta) -> LoadedSession:
    task = Path(meta.path)
    raw = []
    try:
        raw = (task / "api_conversation_history.json").read_text(
            encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        pass
    messages: list[Message] = []
    for i, m in enumerate(_history(task)):
        role = str(m.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        parts = _parts(m)
        if not parts:
            continue
        messages.append(Message(
            line=i, role=role, ts=util.ts_from_ms(m.get("ts")), parts=parts,
            model=meta.model or None))
    return LoadedSession(meta=meta, messages=messages, raw_lines=raw,
                         total_lines=len(raw))
