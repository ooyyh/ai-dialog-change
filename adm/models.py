"""Data model shared between stores and UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Part:
    """One displayable chunk of a message (text / thinking / tool call / result)."""
    kind: str          # text | thinking | tool_call | tool_result | reasoning
    text: str          # display text (args/output pretty-printed for tools)
    raw: Any = None    # original part dict (claude) or payload dict (codex)
    name: str = ""     # tool name for tool_call
    tool_id: str = ""  # tool_use id / call_id


@dataclass
class Message:
    """A display-relevant record of a session file (maps 1:1 to a jsonl line)."""
    line: int                  # 0-based line index in file
    role: str                   # user | assistant | reasoning | tool_call | tool_result | system
    ts: datetime | None
    parts: list[Part] = field(default_factory=list)
    model: str | None = None
    uuid: str = ""
    sidechain: bool = False     # claude subagent traffic
    codex_payload: dict | None = None  # original payload for codex records
    claude_record: dict | None = None  # original record for claude records
    opencode_id: str = ""              # session_message.id for OpenCode rows
    opencode_record: dict | None = None  # parsed OpenCode message data

    @property
    def is_tool_related(self) -> bool:
        if self.role in ("tool_call", "tool_result", "reasoning"):
            return True
        return any(p.kind in ("tool_call", "tool_result") for p in self.parts)

    @property
    def text_parts(self) -> list[Part]:
        return [p for p in self.parts if p.kind == "text"]

    def plain_text(self, limit: int = 4000) -> str:
        chunks = [p.text for p in self.parts if p.kind in ("text", "thinking")]
        s = "\n".join(c for c in chunks if c).strip()
        return s[:limit]

    def tool_ids(self) -> set[str]:
        out: set[str] = set()
        for p in self.parts:
            if p.tool_id:
                out.add(p.tool_id)
        if self.codex_payload and self.role in ("tool_call", "tool_result"):
            cid = self.codex_payload.get("call_id")
            if cid:
                out.add(cid)
        return out


@dataclass
class SessionMeta:
    source: str            # claude | codex | opencode | junie | pi | ...
    id: str
    path: str
    project: str           # display project name
    cwd: str = ""
    title: str = ""
    n_messages: int = 0
    n_user: int = 0
    n_assistant: int = 0
    size: int = 0
    created: datetime | None = None
    updated: datetime | None = None
    model: str = ""
    tokens: int | None = None
    archived: bool = False
    running: bool = False
    subagent: bool = False     # claude projects/<proj>/subagents/*.jsonl
    preview: str = ""
    # local (sidecar) metadata
    starred: bool = False
    tags: list[str] = field(default_factory=list)
    note: str = ""
    custom_title: str = ""

    @property
    def display_title(self) -> str:
        return self.custom_title or self.title or self.preview or self.id[:8]

    @property
    def source_badge(self) -> str:
        from . import sources
        return sources.badge(self.source)

    @property
    def editable(self) -> bool:
        from . import sources
        return sources.is_editable(self.source)


@dataclass
class LoadedSession:
    meta: SessionMeta
    messages: list[Message]      # display stream (file order)
    raw_lines: list[str]         # full file snapshot (keepends)
    total_lines: int
