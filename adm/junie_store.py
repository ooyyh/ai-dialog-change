"""Read-only JetBrains Junie session store.

Junie records one folder per session under ``~/.junie/sessions``.  The
human-readable ``transcript.md`` is the most stable representation; the
``events.jsonl`` stream is useful for timing but is much larger and changes
shape between releases.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import util
from .models import LoadedSession, Message, Part, SessionMeta


def sessions_dir() -> Path:
    env = os.environ.get("JUNIE_HOME")
    if env:
        return Path(env).expanduser() / "sessions"
    return Path.home() / ".junie" / "sessions"


def _summary(dirpath: Path) -> dict:
    try:
        return json.loads((dirpath / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _index() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        for line in (sessions_dir() / "index.jsonl").read_text(
                encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            sid = o.get("sessionId")
            if sid:
                out[sid] = o
    except OSError:
        pass
    return out


def _ms(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _stem_text(path: Path) -> str:
    return util.one_line(path.stem.replace("-", " "), 80)


def scan_junie(progress=None, cache=None) -> list[SessionMeta]:
    base = sessions_dir()
    if not base.is_dir():
        return []
    if progress:
        progress("Junie sessions…")
    index = _index()
    metas: list[SessionMeta] = []
    dirs = sorted(p for p in base.iterdir() if p.is_dir())
    for n, d in enumerate(dirs):
        transcript = d / "transcript.md"
        summary_path = d / "summary.json"
        meta_path = transcript if transcript.is_file() else summary_path
        if not meta_path.is_file():
            continue
        try:
            st = meta_path.stat()
        except OSError:
            continue
        m = cache.get_meta("junie", str(meta_path), st.st_mtime_ns,
                           st.st_size) if cache is not None else None
        if m is None:
            m = _scan_one(d, index)
            if m is None:
                continue
            if cache is not None:
                cache.put_meta("junie", str(meta_path), st.st_mtime_ns,
                               st.st_size, m)
        metas.append(m)
        if progress and n % 20 == 0:
            progress(f"Junie {n + 1}/{len(dirs)}")
    if cache is not None:
        valid = {str(p / ("transcript.md" if (p / "transcript.md").is_file()
                          else "summary.json")) for p in dirs}
        cache.prune_meta("junie", valid)
    return metas


def _scan_one(d: Path, index: dict[str, dict]) -> SessionMeta | None:
    sid = d.name
    s = _summary(d)
    idx = index.get(sid) or {}
    transcript = d / "transcript.md"
    project = str(s.get("projectDir") or idx.get("projectDir") or "")
    title = util.one_line(str(s.get("taskName") or idx.get("taskName") or ""), 120)
    created = util.ts_from_ms(_ms(s.get("createdAt") or idx.get("createdAt")))
    updated = util.ts_from_ms(_ms(s.get("updatedAt") or idx.get("updatedAt")))
    model = ""
    tokens = 0
    usage = ((s.get("mainAgent") or {}).get("modelUsage") or [])
    if usage:
        model = str(usage[0].get("model") or "")
        for u in usage:
            tokens += _ms(u.get("inputTokens")) + _ms(u.get("outputTokens"))
    if not model:
        try:
            model = str((json.loads((d / "settings.json").read_text(
                encoding="utf-8")).get("model") or {}).get("modelId") or "")
        except (OSError, ValueError):
            pass

    messages, n_user, n_asst, first_user = _parse_transcript(transcript)
    if not title:
        title = util.one_line(first_user, 80) or sid
    try:
        size = transcript.stat().st_size if transcript.is_file() else \
            (d / "summary.json").stat().st_size
    except OSError:
        size = 0
    return SessionMeta(
        source="junie", id=sid, path=str(transcript if transcript.is_file()
                                          else d / "summary.json"),
        project=util.path_to_display(project) or "Junie", cwd=project,
        title=title, n_messages=n_user + n_asst, n_user=n_user,
        n_assistant=n_asst, size=size, created=created, updated=updated,
        model=model, tokens=tokens or None,
        preview=util.one_line(first_user, 120),
    )


def _heading(line: str) -> tuple[int, str] | None:
    if line.startswith("## "):
        return 2, line[3:].strip()
    if line.startswith("### "):
        return 3, line[4:].strip()
    return None


def _parse_transcript(path: Path) -> tuple[list[Message], int, int, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0, 0, ""
    lines = text.splitlines()
    msgs: list[Message] = []
    i = 0
    in_fence = False
    current: Message | None = None
    current_text: list[str] = []
    first_user = ""
    n_user = n_asst = 0

    def flush() -> None:
        nonlocal current, current_text
        if current is None:
            return
        body = "\n".join(current_text).strip()
        if body:
            if current.role == "assistant" and current.parts and \
                    current.parts[0].kind == "thinking":
                current.parts[0].text = body
            elif current.parts:
                current.parts[0].text = body
            msgs.append(current)
        current = None
        current_text = []

    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if current is not None:
                current_text.append(line)
            i += 1
            continue
        h = None if in_fence else _heading(line)
        if h:
            level, name = h
            flush()
            if level == 2:
                low = name.lower()
                if low.startswith("user"):
                    current = Message(line=i, role="user", ts=None,
                                      parts=[Part(kind="text", text="")])
                    n_user += 1
                elif low.startswith("task result"):
                    current = Message(line=i, role="assistant", ts=None,
                                      parts=[Part(kind="text", text="")])
                    n_asst += 1
                elif low.startswith("thinking"):
                    current = Message(line=i, role="assistant", ts=None,
                                      parts=[Part(kind="thinking", text="")])
                else:
                    current = None
            else:
                # Tool/status heading: one collapsed block with its output.
                current = Message(line=i, role="tool_call", ts=None,
                                  parts=[Part(kind="tool_call", name=name,
                                              text="")])
            i += 1
            continue
        if current is not None:
            current_text.append(line)
        i += 1
    flush()

    # Tool blocks: move the body into a tool_result part when the heading was
    # a tool ("Ran ...", "Read ...", "Searched ...", "Setup ...").
    for msg in msgs:
        if msg.role == "tool_call" and msg.parts:
            p = msg.parts[0]
            if p.kind == "tool_call" and p.text:
                p.kind = "tool_result"
        if msg.role == "user" and first_user == "":
            texts = [p.text for p in msg.parts if p.kind == "text"]
            first_user = "\n".join(texts).strip()
    return msgs, n_user, n_asst, first_user


def load_session(meta: SessionMeta) -> LoadedSession:
    path = Path(meta.path)
    if path.name != "transcript.md":
        path = path.parent / "transcript.md"
    raw = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        pass
    messages, _, _, _ = _parse_transcript(path)
    for msg in messages:
        if msg.role == "assistant":
            msg.model = meta.model or None
    return LoadedSession(meta=meta, messages=messages, raw_lines=raw,
                         total_lines=len(raw))
