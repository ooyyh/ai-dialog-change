"""Filesystem locations for Claude Code / Codex session data + app config."""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

CLAUDE_DIR = HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CLAUDE_SESSIONS = CLAUDE_DIR / "sessions"  # <pid>.json live-session markers

CODEX_DIR = HOME / ".codex"
CODEX_SESSIONS = CODEX_DIR / "sessions"  # YYYY/MM/DD/rollout-*.jsonl
CODEX_STATE_DB = CODEX_DIR / "state_5.sqlite"
CODEX_THREAD_HISTORY_DB = CODEX_DIR / "thread_history_1.sqlite"
CODEX_LOCKS = CODEX_DIR / "thread-writer-locks"

APP_DIR = Path(os.environ.get("APPDATA", str(HOME))) / "ai-dialog-manager"
BACKUP_DIR = APP_DIR / "backups"
META_FILE = APP_DIR / "meta.json"

BACKUPS_KEEP = 8  # per session


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
