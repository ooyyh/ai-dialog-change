"""Registry of session sources supported by the app.

Claude, Codex and OpenCode are editable.  The remaining sources are scraped
read-only: they are displayed, searched, starred, exported and converted, but
the app never rewrites their native stores.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import SessionMeta


@dataclass(frozen=True)
class SourceInfo:
    id: str
    badge: str
    color: str
    editable: bool = False
    order: int = 100


SOURCES: dict[str, SourceInfo] = {
    "claude": SourceInfo("claude", "Claude", "#5b8cff", True, 10),
    "codex": SourceInfo("codex", "Codex", "#4ec9a8", True, 20),
    "opencode": SourceInfo("opencode", "OpenCode", "#f2a33b", True, 30),
    "junie": SourceInfo("junie", "Junie", "#59c26d", False, 40),
    "pi": SourceInfo("pi", "Pi", "#c586ff", False, 50),
    "gemini": SourceInfo("gemini", "Gemini", "#8a7dff", False, 60),
    "qwen": SourceInfo("qwen", "Qwen", "#6fb3ff", False, 70),
    "goose": SourceInfo("goose", "Goose", "#c9a227", False, 80),
    "cline": SourceInfo("cline", "Cline", "#6cc4a1", False, 90),
    "roo": SourceInfo("roo", "Roo Code", "#ff8a65", False, 100),
    "qoder": SourceInfo("qoder", "Qoder", "#ff7a59", False, 110),
}


def info(source: str) -> SourceInfo:
    return SOURCES.get(source, SourceInfo(source, source.title(), "#9aa4b2"))


def badge(source: str) -> str:
    return info(source).badge


def color(source: str) -> str:
    return info(source).color


def is_editable(source: str) -> bool:
    return info(source).editable


def ordered() -> list[SourceInfo]:
    return sorted(SOURCES.values(), key=lambda s: s.order)


def resume_command(meta: SessionMeta) -> str | None:
    """Shell command that reopens this session, or None for GUI-only tools."""
    sid = meta.id
    return {
        "claude": f"claude --resume {sid}",
        "codex": f"codex resume {sid}",
        "opencode": f"opencode --session {sid}",
        "junie": f"junie --session-id {sid} --resume",
        "pi": f"pi --session {sid}",
        "gemini": f"gemini --resume {sid}",
        "qwen": f"qwen --resume {sid}",
        "goose": f"goose session --resume --name {sid}",
    }.get(meta.source)


def scan_all(progress=None, cache=None) -> list[SessionMeta]:
    """Scan every source; one broken store must never block the others."""
    from . import (claude_store, cline_store, codex_store, gemini_store,
                   goose_store, junie_store, opencode_store, pi_store,
                   qoder_store)

    scanners = (
        ("claude", claude_store.scan_claude),
        ("codex", codex_store.scan_codex),
        ("opencode", opencode_store.scan_opencode),
        ("junie", junie_store.scan_junie),
        ("pi", pi_store.scan_pi),
        ("gemini", lambda progress=None, cache=None: gemini_store.scan_gemini(
            "gemini", progress=progress, cache=cache)),
        ("qwen", lambda progress=None, cache=None: gemini_store.scan_gemini(
            "qwen", progress=progress, cache=cache)),
        ("goose", goose_store.scan_goose),
        ("cline", lambda progress=None, cache=None: cline_store.scan_cline(
            "cline", progress=progress, cache=cache)),
        ("roo", lambda progress=None, cache=None: cline_store.scan_cline(
            "roo", progress=progress, cache=cache)),
        ("qoder", qoder_store.scan_qoder),
    )
    out: list[SessionMeta] = []
    for source, fn in scanners:
        if progress:
            progress(f"scan {badge(source)}…")
        try:
            out.extend(fn(progress=progress, cache=cache))
        except Exception as e:  # noqa: BLE001 — a broken source is non-fatal
            if progress:
                progress(f"{badge(source)} skipped: {e}")
    return out


def load_session(meta: SessionMeta):
    from . import (claude_store, cline_store, codex_store, gemini_store,
                   goose_store, junie_store, opencode_store, pi_store,
                   qoder_store)

    if meta.source == "claude":
        return claude_store.load_claude(meta)
    if meta.source == "codex":
        return codex_store.load_codex(meta)
    if meta.source == "opencode":
        return opencode_store.load_session(meta)
    if meta.source == "junie":
        return junie_store.load_session(meta)
    if meta.source == "pi":
        return pi_store.load_session(meta)
    if meta.source in ("gemini", "qwen"):
        return gemini_store.load_session(meta)
    if meta.source == "goose":
        return goose_store.load_session(meta)
    if meta.source in ("cline", "roo"):
        return cline_store.load_session(meta)
    if meta.source == "qoder":
        return qoder_store.load_session(meta)
    raise ValueError(f"unsupported session source: {meta.source}")
