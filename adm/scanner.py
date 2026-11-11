"""Background scan of all Claude + Codex sessions."""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from . import claude_store, codex_store, local_store
from .models import SessionMeta


class ScanThread(QThread):
    progress = pyqtSignal(str)
    finished_scan = pyqtSignal(list)

    def run(self) -> None:
        metas: list[SessionMeta] = []
        try:
            metas.extend(claude_store.scan_claude(self._prog))
        except Exception as e:  # noqa: BLE001 — scanner must never kill the UI
            self.progress.emit(f"claude scan error: {e}")
        try:
            metas.extend(codex_store.scan_codex(self._prog))
        except Exception as e:  # noqa: BLE001
            self.progress.emit(f"codex scan error: {e}")
        local_store.apply_to(metas)
        def sort_key(m):
            dt = m.updated or m.created
            return dt.timestamp() if dt else 0.0
        metas.sort(key=sort_key, reverse=True)
        self.finished_scan.emit(metas)

    def _prog(self, s: str) -> None:
        self.progress.emit(s)
