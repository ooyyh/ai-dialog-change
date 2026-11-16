"""Consciousness transplant: adapt a session transcript to another AI tool.

The app natively understands Claude Code and Codex transcripts.  This module
turns either of them into a *neutral* conversation (user / assistant text) and
then writes it into the session store of a chosen target:

* ``claude``    ~/.claude/projects/<munged cwd>/<new uuid>.jsonl
* ``codex``     ~/.codex/sessions/YYYY/MM/DD/rollout-*-<uuid7>.jsonl + state DB
* ``opencode``  OpenCode V2 (2.x) via its local HTTP import API
* ``gemini``    ~/.gemini/tmp/<sha256(cwd)>/chats/session-*.json (DeepSeek CLI)
* ``qwen``      ~/.qwen/tmp/<sha256(cwd)>/chats/session-*.json (Qwen Code)
* ``goose``     ~/.local/share/goose/sessions/<id>.json
* ``cline``     VS Code globalStorage/.../saoudrizwan.claude-dev/tasks/<id>/
* ``roo``       VS Code globalStorage/.../rooveterinaryinc.roo-cline/tasks/<id>/
* ``json``      portable transcript.json + transcript.md for any other tool

Tool calls/results and (optionally) reasoning are flattened to readable text
before conversion.  This is deliberate: the target CLI does not know the source
tool schemas, so keeping raw tool_use blocks would make the resumed session
unusable on the next model call.

The source session is never modified.  Every write goes to a brand-new session
with a fresh id and the requested working directory.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import claude_store, codex_store, config, util
from .models import LoadedSession, Message, SessionMeta

TARGETS = ("claude", "codex", "opencode", "gemini", "qwen", "goose",
           "cline", "roo", "json")

# Written into pre-rendered transcripts of an upload bundle. Installers
# replace it with the target working folder at install time. Alphanumeric +
# underscore only, so it needs no JSON/URL escaping while matching.
CWD_PLACEHOLDER = "__ADM_TARGET_CWD__"

# user records that are harness plumbing rather than human input
_INJECT_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command",
    "<system-reminder", "<environment_context", "<permissions instructions",
    "<user_instructions", "<app_instructions", "<collaboration_mode",
    "<turn_aborted", "<skill>", "<agenda_context", "<remote", "<system",
    "Caveat: The messages below were generated",
)


class TransplantError(RuntimeError):
    """Conversion failed with a user-actionable message."""


# ---------------------------------------------------------------- neutral

@dataclass
class NeutralMessage:
    role: str                 # user | assistant
    text: str
    ts: datetime | None = None


@dataclass
class TransplantOptions:
    include_tools: bool = True
    include_reasoning: bool = False
    include_sidechain: bool = False
    tool_arg_limit: int = 4000
    tool_result_limit: int = 8000


@dataclass
class TransplantResult:
    target: str
    session_id: str
    title: str
    cwd: str
    path: str = ""
    n_messages: int = 0
    detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def resume_hint(self) -> str:
        if self.target == "claude":
            return f'cd "{self.cwd}" && claude   ->  /resume'
        if self.target == "codex":
            return f'cd "{self.cwd}" && codex   ->  /resume'
        if self.target == "opencode":
            return f'cd "{self.cwd}" && opencode   (pick "{self.title}")'
        if self.target in ("gemini", "qwen"):
            return f'cd "{self.cwd}" && {self.target} --resume {self.session_id}'
        if self.target == "goose":
            return (f'cd "{self.cwd}" && goose session --resume '
                    f'--name {self.session_id}')
        if self.target in ("cline", "roo"):
            label = "Cline" if self.target == "cline" else "Roo Code"
            return f"open VS Code -> {label} -> History ({self.path})"
        return f"portable transcript: {self.path}"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(truncated {len(text) - limit} chars)"


def _is_injection(text: str) -> bool:
    s = text.lstrip()
    return any(s.startswith(p) for p in _INJECT_PREFIXES)


def _tool_call_text(msg: Message, part, limit: int) -> str:
    name = part.name or msg.role
    args = (part.text or "").strip()
    head = f"[tool: {name}]"
    return head + ("\n" + _clip(args, limit) if args else "")


def _tool_result_text(part, limit: int) -> str:
    body = (part.text or "").strip()
    if not body:
        return "[tool result]"
    return "[tool result]\n" + _clip(body, limit)


def _clean_api_errors(msg: Message) -> bool:
    rec = msg.claude_record or {}
    if rec.get("isApiErrorMessage"):
        return True
    if (msg.model or "") == "<synthetic>":
        text = "\n".join(p.text for p in msg.parts if p.kind == "text")
        return text.startswith("API Error:")
    return False


def build_transcript(messages: list[Message],
                     options: TransplantOptions | None = None) -> list[NeutralMessage]:
    """Turn a parsed source stream into granular user/assistant turns.

    Do **not** merge adjacent assistant messages into one giant block.  A long
    agent run has text, thinking, tool calls and tool results interleaved;
    each becomes its own neutral message so target tools render separate
    bubbles instead of one multi-hundred-KB wall of text.
    """
    opt = options or TransplantOptions()
    out: list[NeutralMessage] = []

    def emit(role: str, text: str, ts) -> None:
        text = (text or "").strip()
        if text:
            out.append(NeutralMessage(role=role, text=text, ts=ts))

    def prefix_reasoning(text: str) -> str:
        if not text.strip():
            return ""
        return "[thinking]\n" + text.strip()

    def assistant_chunks(m: Message) -> list[str]:
        """Split one source assistant record into readable chunks.

        A tool call and its matching result stay together in one chunk;
        unrelated text/thinking/tool traffic becomes separate messages.
        """
        chunks: list[str] = []
        pending: list[str] | None = None   # [text, call_id]
        for p in m.parts:
            kind = p.kind
            if kind == "text" and (p.text or "").strip():
                if pending:
                    chunks.append(pending[0])
                    pending = None
                chunks.append(p.text.strip())
            elif kind in ("thinking", "reasoning"):
                if not opt.include_reasoning or not (p.text or "").strip():
                    continue
                if pending:
                    chunks.append(pending[0])
                    pending = None
                chunks.append(prefix_reasoning(p.text))
            elif kind == "tool_call" and opt.include_tools:
                if pending:
                    chunks.append(pending[0])
                pending = [_tool_call_text(m, p, opt.tool_arg_limit),
                           p.tool_id or ""]
            elif kind == "tool_result" and opt.include_tools:
                result = _tool_result_text(p, opt.tool_result_limit)
                if pending and (not p.tool_id or not pending[1]
                                or p.tool_id == pending[1]):
                    pending[0] = pending[0].rstrip() + "\n\n" + result
                    chunks.append(pending[0])
                    pending = None
                else:
                    if pending:
                        chunks.append(pending[0])
                        pending = None
                    chunks.append(result)
        if pending:
            chunks.append(pending[0])
        return chunks

    for m in messages:
        if m.sidechain and not opt.include_sidechain:
            continue
        if m.role == "system" or _clean_api_errors(m):
            continue
        sc_prefix = "[subagent]\n" if m.sidechain else ""
        texts = [p.text for p in m.parts
                 if p.kind == "text" and (p.text or "").strip()]
        thinking = [p.text for p in m.parts
                    if p.kind in ("thinking", "reasoning") and (p.text or "").strip()]
        results = [p for p in m.parts if p.kind == "tool_result"]
        calls = [p for p in m.parts if p.kind == "tool_call"]

        if m.role == "user":
            if results and opt.include_tools:  # Claude tool_result record
                for p in results:
                    emit("assistant",
                         sc_prefix + _tool_result_text(p, opt.tool_result_limit),
                         m.ts)
            text = "\n\n".join(texts)
            if text and not _is_injection(text):
                emit("user", sc_prefix + text, m.ts)
            continue

        if m.role == "assistant":
            for chunk in assistant_chunks(m):
                emit("assistant", sc_prefix + chunk, m.ts)
            continue

        if m.role == "reasoning":
            if opt.include_reasoning and thinking:
                emit("assistant", sc_prefix + prefix_reasoning("\n".join(thinking)),
                     m.ts)
            continue

        if m.role == "tool_result":
            if opt.include_tools:
                for p in results:
                    emit("assistant",
                         sc_prefix + _tool_result_text(p, opt.tool_result_limit),
                         m.ts)
            continue

        if m.role == "tool_call":
            if opt.include_tools:
                for p in calls:
                    emit("assistant",
                         sc_prefix + _tool_call_text(m, p, opt.tool_arg_limit),
                         m.ts)
            continue

    return out


# ---------------------------------------------------------------- helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return _now_iso()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ms(dt: datetime | None) -> int:
    if dt is None:
        return int(time.time() * 1000)
    return int(dt.timestamp() * 1000)


def _safe_name(s: str, limit: int = 60) -> str:
    s = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", s or "").strip(" ._")
    return (s[:limit].rstrip(" ._") or "session")


def _munge(cwd: str) -> str:
    """Claude Code project-dir encoding: every non-alphanumeric -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _unique_dir(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while True:
        q = p.with_name(f"{p.name}-{n}")
        if not q.exists():
            return q
        n += 1


def _tool_version(cmd: str, pattern: str, fallback: str) -> str:
    exe = shutil.which(cmd)
    if not exe:
        return fallback
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=6, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return fallback
    m = re.search(pattern, out or "")
    return m.group(1) if m else fallback


_version_cache: dict[str, str] = {}


def _claude_version() -> str:
    if "claude" not in _version_cache:
        _version_cache["claude"] = _tool_version(
            "claude", r"(\d+\.\d+\.\d+)", "2.1.285")
    return _version_cache["claude"]


def _codex_version() -> str:
    if "codex" not in _version_cache:
        _version_cache["codex"] = _tool_version(
            "codex", r"(\d+\.\d+\.\d+)", "0.166.0")
    return _version_cache["codex"]


def _claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _ensure_cwd(cwd: str) -> str:
    cwd = os.path.abspath(os.path.expanduser(cwd))
    Path(cwd).mkdir(parents=True, exist_ok=True)
    return cwd


# ---------------------------------------------------------------- loaders

def load_source(meta: SessionMeta) -> LoadedSession:
    from . import sources
    return sources.load_session(meta)


def _meta_from_file(path: Path) -> SessionMeta:
    """Build a SessionMeta for a transcript path (CLI convenience)."""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        head: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                head.append(json.loads(line))
            except ValueError:
                continue
            if len(head) >= 30:
                break
        first = head[0] if head else {}
        if first.get("type") == "session_meta":
            p = first.get("payload") or {}
            return SessionMeta(source="codex", id=p.get("session_id") or p.get("id")
                               or path.stem, path=str(path),
                               cwd=util.strip_verbatim(p.get("cwd") or ""),
                               project=util.path_to_display(p.get("cwd") or str(path.parent)))
        cwd = next((r["cwd"] for r in head if isinstance(r.get("cwd"), str)), "")
        return SessionMeta(source="claude", id=path.stem, path=str(path),
                           project=path.parent.name, cwd=cwd)
    raise TransplantError(f"not a transcript file: {path}")


def find_session(source: str) -> SessionMeta:
    """Resolve a session id / id prefix across every supported source."""
    from . import sources
    from .scan_cache import ScanCache
    cache = ScanCache()
    metas = sources.scan_all(cache=cache)
    cache.save()
    hits = [m for m in metas
            if m.id == source or m.id.lower().startswith(source.lower())]
    if not hits:
        raise TransplantError(f"session not found: {source}")
    if len(hits) > 1:
        listing = "\n".join(f"  {h.source}  {h.id}  {h.display_title[:60]}"
                            for h in hits[:10])
        raise TransplantError(f"ambiguous session id {source!r}:\n{listing}")
    return hits[0]


# ---------------------------------------------------------------- writers

def write_transcript(messages: list[NeutralMessage], title: str, cwd: str,
                     target: str, *, source: str = "claude", source_name: str = "",
                     model: str = "", out_dir: str | Path | None = None,
                     options: TransplantOptions | None = None) -> TransplantResult:
    if target not in TARGETS:
        raise TransplantError(f"unknown target: {target}")
    if not messages:
        raise TransplantError("no convertible text messages in this session")
    title = " ".join((title or "").split())[:200] or "transplanted session"
    cwd = _ensure_cwd(cwd)
    if target == "claude":
        return _write_claude(messages, title, cwd, model=model)
    if target == "codex":
        return _write_codex(messages, title, cwd, model=model)
    if target == "opencode":
        return _write_opencode(messages, title, cwd)
    if target in ("gemini", "qwen"):
        default = "gemini-2.5-pro" if target == "gemini" else "qwen3-coder-plus"
        return _write_gemini(messages, title, cwd, tool=target,
                             model=model or default)
    if target == "goose":
        return _write_goose(messages, title, cwd)
    if target in ("cline", "roo"):
        return _write_cline(messages, title, cwd, tool=target)
    return _write_generic(messages, title, cwd, source, source_name,
                          Path(out_dir) if out_dir else Path(cwd))


def transplant(meta: SessionMeta, target: str, cwd: str | None = None,
               title: str | None = None,
               options: TransplantOptions | None = None,
               out_dir: str | Path | None = None) -> TransplantResult:
    session = load_source(meta)
    messages = build_transcript(session.messages, options)
    return write_transcript(
        messages, title or meta.display_title,
        cwd or meta.cwd or str(Path.home()), target,
        source=meta.source, source_name=meta.source_badge,
        model=meta.model or "", out_dir=out_dir, options=options)


# -- Claude Code -----------------------------------------------------------

def build_claude_session(messages: list[NeutralMessage], title: str, cwd: str,
                         *, model: str = "", sid: str | None = None
                         ) -> tuple[str, str]:
    """Build a Claude Code transcript: (session_id, jsonl_text).

    Kept separate from the writer so callers (e.g. the upload bundle) can
    generate the transcript without touching this machine's session store.
    """
    sid = sid or str(uuidlib.uuid4())
    version = _claude_version()
    model = model if model and model.startswith("claude") else "claude-sonnet-4-5"
    records: list[dict] = [
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "permission-mode", "permissionMode": "bypassPermissions",
         "sessionId": sid},
    ]
    parent = None
    last_uuid = ""
    for m in messages:
        uid = str(uuidlib.uuid4())
        ts = _iso(m.ts)
        base = {
            "parentUuid": parent, "isSidechain": False, "userType": "external",
            "entrypoint": "cli", "cwd": cwd, "sessionId": sid,
            "version": version, "gitBranch": "HEAD", "uuid": uid, "timestamp": ts,
        }
        if m.role == "user":
            rec = dict(base)
            rec.update({
                "type": "user", "promptId": str(uuidlib.uuid4()),
                "message": {"role": "user", "content": m.text},
            })
        else:
            rec = dict(base)
            rec.update({
                "type": "assistant",
                "message": {
                    "id": f"msg_{uuidlib.uuid4().hex[:24]}",
                    "type": "message", "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": m.text}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            })
        records.append(rec)
        parent = uid
        last_uuid = uid

    records.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
    last_user = next((m.text for m in reversed(messages) if m.role == "user"), "")
    records.append({
        "type": "last-prompt",
        "lastPrompt": util.one_line(last_user, 500),
        "leafUuid": last_uuid,
        "sessionId": sid,
    })
    return sid, "\n".join(util.dumps_claude(r) for r in records) + "\n"


def _write_claude(messages: list[NeutralMessage], title: str, cwd: str,
                  *, model: str) -> TransplantResult:
    sid, text = build_claude_session(messages, title, cwd, model=model)
    path = _claude_home() / "projects" / _munge(cwd) / f"{sid}.jsonl"
    _atomic_write(path, text)
    return TransplantResult(target="claude", session_id=sid, title=title, cwd=cwd,
                            path=str(path), n_messages=len(messages))


# -- Codex -----------------------------------------------------------------

def _codex_defaults() -> tuple[str, str, str, str]:
    """(model, provider, sandbox, approval) from the newest thread, best effort.

    Copying the existing thread's sandbox/approval avoids silently granting more
    permissions to an imported session than the user normally runs with.
    """
    try:
        con = codex_store._connect(config.CODEX_STATE_DB, readonly=True)
        try:
            row = con.execute(
                "SELECT * FROM threads ORDER BY updated_at_ms DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if row:
            keys = set(row.keys())
            return (
                row["model"] or "gpt-5",
                row["model_provider"] or "openai",
                (row["sandbox_policy"] if "sandbox_policy" in keys else "")
                or "workspace-write",
                (row["approval_mode"] if "approval_mode" in keys else "")
                or "on-request",
            )
    except sqlite3.Error:
        pass
    return ("gpt-5", "openai", "workspace-write", "on-request")


def _register_codex_thread(sid: str, rollout: Path, cwd: str, title: str,
                           preview: str, model: str, provider: str,
                           sandbox: str, approval: str) -> bool:
    """Insert the thread row so `codex` lists the imported rollout under /resume."""
    db = config.CODEX_STATE_DB
    if not db.exists():
        return False
    now = int(time.time())
    now_ms = int(time.time() * 1000)
    try:
        con = sqlite3.connect(str(db), timeout=8)
        try:
            info = list(con.execute("PRAGMA table_info(threads)"))
            if not info:
                return False
            cols = [r[1] for r in info]
            required = {r[1]: (r[2] or "", r[3], r[4]) for r in info}
            vals = {
                "id": sid, "rollout_path": str(rollout), "cwd": cwd,
                "title": title, "archived": 0, "archived_at": None,
                "created_at": now, "updated_at": now,
                "created_at_ms": now_ms, "updated_at_ms": now_ms,
                "recency_at": now, "recency_at_ms": now_ms,
                "is_pinned": 0, "has_user_event": 1, "tokens_used": 0,
                "first_user_message": preview, "preview": preview,
                "thread_section_id": None, "project_id": None,
                "section_position": None, "section_entered_at_ms": None,
                "attached_thread_parent_id": None, "source": "cli",
                "model": model, "model_provider": provider,
                "sandbox_policy": sandbox, "approval_mode": approval,
            }
            use = [c for c in cols if c in vals and vals[c] is not None]
            for name, (typ, notnull, dflt) in required.items():
                if name in use or not notnull or dflt is not None:
                    continue
                typ = typ.upper()
                vals[name] = "" if ("CHAR" in typ or "TEXT" in typ) else 0
                use.append(name)
            use = [c for c in cols if c in use]
            q = (f"INSERT OR REPLACE INTO threads ({','.join(use)}) "
                 f"VALUES ({','.join('?' * len(use))})")
            con.execute(q, [vals[c] for c in use])
            con.commit()
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


def build_codex_rollout(messages: list[NeutralMessage], title: str, cwd: str,
                        *, model: str = "", sid: str | None = None,
                        provider: str = "", ts: datetime | None = None
                        ) -> tuple[str, str, str]:
    """Build a Codex rollout: (session_id, file_name, jsonl_text).

    ``cwd`` may be the placeholder; the caller substitutes the real working
    folder at install time.
    """
    sid = sid or codex_store.new_session_id()
    ts0 = ts or datetime.now()
    model = model or ""
    if model and not re.match(r"[A-Za-z0-9._:-]+$", model):
        model = ""
    version = _codex_version()
    records: list[dict] = [{
        "timestamp": _iso(ts0), "type": "session_meta",
        "payload": {
            "session_id": sid, "id": sid, "timestamp": _iso(ts0),
            "cwd": cwd, "originator": "codex-tui", "cli_version": version,
            "source": "cli", "thread_source": "user",
            "model_provider": provider or "openai",
        },
    }]
    for m in messages:
        t = _iso(m.ts)
        if m.role == "user":
            records.append({
                "timestamp": t, "type": "response_item",
                "payload": {
                    "type": "message", "id": f"msg_{uuidlib.uuid4()}",
                    "role": "user",
                    "content": [{"type": "input_text", "text": m.text}],
                },
            })
            records.append({
                "timestamp": t, "type": "event_msg",
                "payload": {
                    "type": "user_message", "message": m.text,
                    "images": [], "local_images": [], "text_elements": [],
                },
            })
        else:
            records.append({
                "timestamp": t, "type": "response_item",
                "payload": {
                    "type": "message", "id": f"msg_{uuidlib.uuid4()}",
                    "role": "assistant", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": m.text}],
                },
            })
            records.append({
                "timestamp": t, "type": "event_msg",
                "payload": {"type": "agent_message", "message": m.text},
            })
    name = f"rollout-{ts0:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
    return sid, name, "\n".join(util.dumps_codex(r) for r in records) + "\n"


def _write_codex(messages: list[NeutralMessage], title: str, cwd: str,
                 *, model: str) -> TransplantResult:
    def_model, provider, sandbox, approval = _codex_defaults()
    model = model or def_model
    if model and not re.match(r"[A-Za-z0-9._:-]+$", model):
        model = def_model
    now = datetime.now()
    sid, name, text = build_codex_rollout(messages, title, cwd, model=model,
                                          provider=provider, ts=now)
    folder = _codex_home() / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    rollout = folder / name
    _atomic_write(rollout, text)
    first_user = next((m.text for m in messages if m.role == "user"), "")
    preview = util.one_line(first_user, 200)
    ok = _register_codex_thread(sid, rollout, cwd, title, preview, model, provider,
                                sandbox, approval)
    result = TransplantResult(target="codex", session_id=sid, title=title,
                              cwd=cwd, path=str(rollout), n_messages=len(messages))
    if not ok:
        result.warnings.append(
            "state_5.sqlite registration failed; the rollout exists but "
            "`codex /resume` may not list it until the database is available")
    return result


# -- OpenCode V2 -----------------------------------------------------------

def _opencode_config_dir() -> Path:
    env = os.environ.get("XDG_CONFIG_HOME")
    return (Path(env) if env else Path.home() / ".config") / "opencode"


def _opencode_data_dir() -> Path:
    env = os.environ.get("XDG_DATA_HOME")
    return (Path(env) if env else Path.home() / ".local" / "share") / "opencode"


def _opencode_service() -> tuple[str, str]:
    """(base_url, password) for the running OpenCode service."""
    cli = shutil.which("opencode")
    if not cli:
        raise TransplantError(
            "OpenCode CLI not found. Install OpenCode or pick another target.")
    try:
        out = subprocess.run([cli, "api", "get", "/api/info"],
                             capture_output=True, text=True, timeout=45,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        raise TransplantError(f"cannot reach OpenCode: {e}") from e
    if out.returncode != 0:
        raise TransplantError(
            "OpenCode service is not reachable. Start `opencode` once, then retry.\n"
            + (out.stderr or out.stdout or "").strip()[:500])
    try:
        info = json.loads(out.stdout)
    except ValueError:
        raise TransplantError("unexpected `opencode api` output: "
                              + (out.stdout or "")[:300])
    urls = info.get("urls") or []
    if not urls:
        raise TransplantError("OpenCode service did not report a URL")
    password = os.environ.get("OPENCODE_PASSWORD") \
        or os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
    if not password:
        cfg = _opencode_config_dir() / "service.json"
        try:
            password = json.loads(cfg.read_text(encoding="utf-8")).get("password", "")
        except (OSError, ValueError):
            password = ""
    if not password:
        raise TransplantError("OpenCode service password not found "
                              "(~/.config/opencode/service.json)")
    return urls[0].rstrip("/"), password


def _opencode_defaults() -> tuple[str, dict]:
    """(agent, model) from the newest OpenCode session, best effort."""
    db = _opencode_data_dir() / "opencode.db"
    fallback = ("build", {"id": "claude-sonnet-4-5", "providerID": "anthropic"})
    if not db.exists():
        return fallback
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=4)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT agent, model FROM session_v2 "
                "ORDER BY time_updated DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row:
            return fallback
        agent = row["agent"] or "build"
        model = json.loads(row["model"] or "{}")
        if not model.get("id") or not model.get("providerID"):
            return fallback
        return agent, model
    except (sqlite3.Error, ValueError):
        return fallback


def _opencode_http(base: str, password: str, method: str, path: str,
                   body: dict | None = None, timeout: int = 120):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
        if body is not None else None
    token = base64.b64encode(("opencode:" + password).encode()).decode()
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": "Basic " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise TransplantError(f"OpenCode import failed ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise TransplantError(
            "cannot connect to the OpenCode service. Start `opencode` once, "
            f"then retry.\n{e}") from e


def build_opencode_payload(messages: list[NeutralMessage], title: str, cwd: str,
                           *, agent: str | None = None, model: dict | None = None,
                           sid: str | None = None) -> dict:
    """Build the body of POST /api/experimental/session/import."""
    agent, model = (agent, model) if agent and model else _opencode_defaults()
    sid = sid or "ses_" + uuidlib.uuid4().hex
    now_ms = int(time.time() * 1000)
    payload_messages: list[dict] = []
    for m in messages:
        mid = "msg_" + uuidlib.uuid4().hex
        ts = _ms(m.ts)
        if m.role == "user":
            payload_messages.append({
                "id": mid, "time": {"created": ts}, "text": m.text,
                "files": [], "agents": [], "type": "user",
            })
        else:
            payload_messages.append({
                "id": mid,
                "time": {"created": ts, "completed": ts + 1},
                "type": "assistant", "agent": agent, "model": model,
                "content": [{"type": "text", "text": m.text}],
                "finish": "stop", "rawFinish": "stop",
            })
    return {
        "info": {
            "id": sid, "projectID": "0" * 40, "agent": agent, "model": model,
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
            "outcome": "succeeded",
            "time": {"created": now_ms, "updated": now_ms,
                     "idle": now_ms, "viewed": now_ms},
            "title": title,
            "location": {"directory": cwd},
        },
        "messages": payload_messages,
        "location": {"directory": cwd},
    }


def _write_opencode(messages: list[NeutralMessage], title: str,
                    cwd: str) -> TransplantResult:
    base, password = _opencode_service()
    sid, model = None, None
    agent, model = _opencode_defaults()
    payload = build_opencode_payload(messages, title, cwd, agent=agent, model=model)
    sid = payload["info"]["id"]
    status, data = _opencode_http(base, password, "POST",
                                  "/api/experimental/session/import", payload)
    info = (data or {}).get("data") or {}
    return TransplantResult(
        target="opencode", session_id=info.get("id") or sid,
        title=info.get("title") or title, cwd=cwd,
        path=(info.get("location") or {}).get("directory") or cwd,
        n_messages=len(messages),
        detail=f"projectID={info.get('projectID', '')}")


# -- Gemini CLI / Qwen Code ------------------------------------------------

def _project_hash(cwd: str) -> str:
    """DeepSeek CLI / DeepSeek Code project key: sha256(cwd) hex."""
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()


def build_gemini_session(messages: list[NeutralMessage], title: str, cwd: str,
                         *, model: str = "", sid: str | None = None,
                         project_hash: str | None = None) -> dict:
    """Session JSON as written by DeepSeek CLI's ChatRecordingService."""
    sid = sid or str(uuidlib.uuid4())
    records: list[dict] = []
    for m in messages:
        ts = _iso(m.ts)
        if m.role == "user":
            records.append({
                "id": str(uuidlib.uuid4()), "timestamp": ts,
                "type": "user", "content": m.text,
            })
        else:
            records.append({
                "id": str(uuidlib.uuid4()), "timestamp": ts,
                "type": "gemini", "content": m.text,
                "model": model or "gemini-2.5-pro",
                "tokens": {"input": 0, "output": 0, "cached": 0,
                           "thoughts": 0, "tool": 0, "total": 0},
            })
    start = _iso(messages[0].ts) if messages else _now_iso()
    return {
        "sessionId": sid,
        "projectHash": project_hash or _project_hash(cwd),
        "startTime": start,
        "lastUpdated": _now_iso(),
        "messages": records,
    }


def _gemini_home(tool: str) -> Path:
    env = os.environ.get("GEMINI_CLI_HOME" if tool == "gemini"
                         else "QWEN_HOME")
    return Path(env).expanduser() if env else Path.home() / f".{tool}"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _write_gemini(messages: list[NeutralMessage], title: str, cwd: str,
                  *, tool: str, model: str) -> TransplantResult:
    sid = str(uuidlib.uuid4())
    doc = build_gemini_session(messages, title, cwd, model=model, sid=sid)
    dest = (_gemini_home(tool) / "tmp" / doc["projectHash"] / "chats"
            / f"session-{_utc_stamp()}-{sid}.json")
    _atomic_write(dest, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return TransplantResult(target=tool, session_id=sid, title=title, cwd=cwd,
                            path=str(dest), n_messages=len(messages))


# -- Goose -----------------------------------------------------------------

def build_goose_session(messages: list[NeutralMessage], title: str, cwd: str,
                        *, sid: str | None = None) -> dict:
    """Session JSON as stored in ~/.local/share/goose/sessions/<id>.json."""
    sid = sid or datetime.now().strftime("%Y%m%d_%H%M%S")
    out: list[dict] = []
    for m in messages:
        ts = m.ts if m.ts is not None else datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "role": "user" if m.role == "user" else "assistant",
            "created": int(ts.timestamp()),
            "content": [{"type": "text", "text": m.text}],
        })
    return {
        "id": sid,
        "description": title,
        "working_dir": cwd,
        "extension_data": {},
        "conversation": {"messages": out},
        "message_count": len(out),
        "created_at": _iso(messages[0].ts) if messages else _now_iso(),
        "updated_at": _now_iso(),
    }


def _goose_home() -> Path:
    env = os.environ.get("GOOSE_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA")
                       or Path.home() / "AppData" / "Roaming")
        cands = [appdata / "Block" / "goose" / "data" / "sessions",
                 appdata / "goose" / "data" / "sessions",
                 appdata / "Block" / "goose" / "sessions",
                 appdata / "goose" / "sessions"]
        for c in cands:
            if c.exists() or c.parent.exists():
                return c
        return cands[0]
    base = Path(os.environ.get("XDG_DATA_HOME")
                or Path.home() / ".local" / "share")
    return base / "goose" / "sessions"


def _write_goose(messages: list[NeutralMessage], title: str, cwd: str
                 ) -> TransplantResult:
    doc = build_goose_session(messages, title, cwd)
    dest = _goose_home() / f"{doc['id']}.json"
    _atomic_write(dest, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return TransplantResult(target="goose", session_id=doc["id"], title=title,
                            cwd=cwd, path=str(dest), n_messages=len(messages))


# -- Cline / Roo Code (VS Code extensions) ---------------------------------

def build_cline_task(messages: list[NeutralMessage], title: str, cwd: str,
                     *, sid: str | None = None
                     ) -> tuple[str, list[dict], list[dict], dict]:
    """(task_id, api_conversation_history, ui_messages, task_metadata)."""
    sid = sid or str(int(time.time() * 1000))
    history: list[dict] = []
    ui: list[dict] = []
    first_user = True
    for idx, m in enumerate(messages):
        ts = _ms(m.ts)
        if m.role == "user":
            history.append({"role": "user", "ts": ts,
                            "content": [{"type": "text", "text": m.text}]})
            ui.append({"ts": ts, "type": "say",
                       "say": "task" if first_user else "user_feedback",
                       "text": m.text, "conversationHistoryIndex": idx})
            first_user = False
        else:
            history.append({"role": "assistant", "ts": ts,
                            "content": [{"type": "text", "text": m.text}]})
            ui.append({"ts": ts, "type": "say", "say": "text",
                       "text": m.text, "conversationHistoryIndex": idx})
    meta = {"files_in_context": [], "model_usage": [],
            "environment_history": []}
    return sid, history, ui, meta


def _vscode_global_storage() -> Path:
    env = os.environ.get("ADM_VSCODE_GLOBAL_STORAGE")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA")
                    or Path.home() / "AppData" / "Roaming")
        stable = base / "Code" / "User" / "globalStorage"
        insiders = base / "Code - Insiders" / "User" / "globalStorage"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        stable = base / "Code" / "User" / "globalStorage"
        insiders = base / "Code - Insiders" / "User" / "globalStorage"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME")
                    or Path.home() / ".config")
        stable = base / "Code" / "User" / "globalStorage"
        insiders = base / "Code - Insiders" / "User" / "globalStorage"
    if not stable.exists() and insiders.exists():
        return insiders
    return stable


def _cline_ext(tool: str) -> str:
    return ("saoudrizwan.claude-dev" if tool == "cline"
            else "rooveterinaryinc.roo-cline")


def _write_cline(messages: list[NeutralMessage], title: str, cwd: str,
                 *, tool: str) -> TransplantResult:
    sid, history, ui, meta = build_cline_task(messages, title, cwd)
    dest = _vscode_global_storage() / _cline_ext(tool) / "tasks" / sid
    _atomic_write(dest / "api_conversation_history.json",
                  json.dumps(history, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(dest / "ui_messages.json",
                  json.dumps(ui, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(dest / "task_metadata.json",
                  json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return TransplantResult(target=tool, session_id=sid, title=title, cwd=cwd,
                            path=str(dest), n_messages=len(messages))


# -- generic ---------------------------------------------------------------

def build_generic_transcript(messages: list[NeutralMessage], title: str, cwd: str,
                             source: str, source_name: str) -> tuple[dict, str, str]:
    """Portable transcript for any tool: (json_doc, markdown, readme)."""
    data = {
        "format": "ai-dialog-transcript",
        "version": 1,
        "created": _now_iso(),
        "source": source,
        "source_name": source_name,
        "title": title,
        "cwd": cwd,
        "messages": [
            {"role": m.role, "text": m.text, "time": _iso(m.ts)}
            for m in messages
        ],
    }
    md = [f"# {title}", "", f"- source: {source_name or source}",
          f"- working directory: `{cwd}`", ""]
    for m in messages:
        md.append("## " + ("User" if m.role == "user" else "Assistant"))
        md.append("")
        md.append(m.text)
        md.append("")
    readme = f"""# Portable transcript · {title}

This folder contains the conversation in two neutral formats:

- `transcript.json` — structured messages (role / text / time)
- `transcript.md` — human-readable Markdown

Give either file to the target AI tool or agent. It carries no tool-specific
schema, so anything that can read JSON or Markdown can continue the work.

- source tool: {source_name or source}
- original working directory: `{cwd}`
- messages: {len(messages)}
- exported: {data['created']}

The source session was not modified.
"""
    return data, "\n".join(md).rstrip() + "\n", readme


def _write_generic(messages: list[NeutralMessage], title: str, cwd: str,
                   source: str, source_name: str, out_dir: Path) -> TransplantResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    folder = _unique_dir(out_dir / f"{_safe_name(title)}-{uuidlib.uuid4().hex[:8]}")
    folder.mkdir(parents=True)
    data, md, readme = build_generic_transcript(
        messages, title, cwd, source, source_name)
    _atomic_write(folder / "transcript.json",
                  json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(folder / "transcript.md", md)
    _atomic_write(folder / "README.md", readme)
    return TransplantResult(target="json", session_id=folder.name, title=title,
                            cwd=cwd, path=str(folder), n_messages=len(messages))


# ---------------------------------------------------------------- neutral readers

def transcript_from_opencode_export(data: dict,
                                    options: TransplantOptions | None = None
                                    ) -> tuple[str, str, list[NeutralMessage]]:
    """Read `GET /api/experimental/session/<id>/export` (or its .data) payload."""
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    info = data.get("info") or {}
    raw_messages = data.get("messages") or []
    opt = options or TransplantOptions()
    out: list[NeutralMessage] = []
    for m in raw_messages:
        typ = m.get("type")
        ts = util.ts_from_ms((m.get("time") or {}).get("created"))
        if typ == "user":
            text = (m.get("text") or "").strip()
            if text and not _is_injection(text):
                out.append(NeutralMessage("user", text, ts))
        elif typ == "assistant":
            chunks: list[str] = []
            for part in m.get("content") or []:
                pt = part.get("type")
                if pt == "text" and (part.get("text") or "").strip():
                    chunks.append(part["text"])
                elif pt == "reasoning" and opt.include_reasoning \
                        and (part.get("text") or "").strip():
                    chunks.append("[thinking]\n" + part["text"].strip())
                elif pt == "tool" and opt.include_tools:
                    state = part.get("state") or {}
                    args = state.get("input")
                    args_text = args if isinstance(args, str) else \
                        json.dumps(args, ensure_ascii=False, indent=2) \
                        if args else ""
                    chunks.append(
                        f"[tool: {part.get('name') or '?'}]\n"
                        + _clip(args_text or "", opt.tool_arg_limit))
                    content = state.get("content") or []
                    body = ""
                    if isinstance(content, str):
                        body = content
                    elif isinstance(content, list):
                        body = "\n".join(
                            c.get("text") or "" for c in content
                            if isinstance(c, dict) and c.get("type") == "text")
                    if body:
                        chunks.append("[tool result]\n"
                                      + _clip(body, opt.tool_result_limit))
                    elif state.get("status") == "error":
                        chunks.append("[tool result]\n"
                                      + _clip(json.dumps(state.get("error") or {},
                                                         ensure_ascii=False),
                                              opt.tool_result_limit))
            text = "\n\n".join(chunks).strip()
            if text:
                if out and out[-1].role == "assistant":
                    out[-1].text += "\n\n" + text
                else:
                    out.append(NeutralMessage("assistant", text, ts))
    title = info.get("title") or "OpenCode session"
    cwd = (info.get("location") or {}).get("directory") or ""
    return title, cwd, out


def transcript_from_generic(data: dict) -> tuple[str, str, list[NeutralMessage]]:
    if data.get("format") != "ai-dialog-transcript":
        raise TransplantError("not an ai-dialog transcript JSON")
    out = [NeutralMessage(m.get("role") or "user", m.get("text") or "",
                          util.parse_iso(m.get("time")))
           for m in data.get("messages") or []]
    out = [m for m in out if m.text.strip()]
    return data.get("title") or "transcript", data.get("cwd") or "", out
