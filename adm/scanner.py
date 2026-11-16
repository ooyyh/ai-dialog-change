"""Background scan of every supported session source."""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from . import local_store, sources
from .scan_cache import ScanCache


class ScanThread(QThread):
    progress = pyqtSignal(str)
    finished_scan = pyqtSignal(list)

    def run(self) -> None:
        metas = []
        cache = ScanCache()
        try:
            metas = sources.scan_all(self._prog, cache)
        except Exception as e:  # noqa: BLE001 — never kill the UI
            self.progress.emit(f"scan error: {e}")
        finally:
            cache.save()
        local_store.apply_to(metas)

        def sort_key(m):
            dt = m.updated or m.created
            return dt.timestamp() if dt else 0.0
        metas.sort(key=sort_key, reverse=True)
        self.finished_scan.emit(metas)

    def _prog(self, s: str) -> None:
        self.progress.emit(s)
