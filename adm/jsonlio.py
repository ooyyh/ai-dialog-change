"""Line-preserving editing of session .jsonl files.

Every save: timestamped backup -> verify the on-disk file still matches the
snapshot taken at load (external-change guard) -> keep any lines the CLI
appended since load -> atomic replace.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import config


class FileChangedError(Exception):
    """The file was modified on disk since we loaded it."""


def read_lines(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.readlines()


def make_backup(path: str | Path, source: str, sid: str) -> Path | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    d = config.BACKUP_DIR / source / sid
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    bak = d / f"{stamp}.jsonl"
    bak.write_bytes(raw)
    _prune(d)
    return bak


def _prune(d: Path) -> None:
    try:
        baks = sorted(d.glob("*.jsonl"))
        for old in baks[: max(0, len(baks) - config.BACKUPS_KEEP)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


class SessionEditor:
    """Accumulates line edits against a snapshot; saves atomically."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lines = read_lines(path)     # snapshot with original endings
        self.n_snapshot = len(self.lines)
        self._deleted: set[int] = set()
        self._replaced: dict[int, str] = {}
        self._appended: list[str] = []
        self.dirty = False

    # -- mutate -----------------------------------------------------------
    def replace_line(self, idx: int, new_text: str, newline: str = "\n") -> None:
        if not new_text.endswith("\n"):
            new_text += newline
        self._replaced[idx] = new_text
        self.dirty = True

    def delete_line(self, idx: int) -> None:
        self._deleted.add(idx)
        self.dirty = True

    def delete_lines(self, idxs) -> None:
        for i in idxs:
            self.delete_line(i)

    def append_line(self, text: str, newline: str = "\n") -> None:
        if not text.endswith("\n"):
            text += newline
        self._appended.append(text)
        self.dirty = True

    # -- save -------------------------------------------------------------
    def _current_tail(self) -> list[str]:
        """Lines appended to the file by the CLI since our snapshot."""
        cur = read_lines(self.path)
        if len(cur) < self.n_snapshot:
            raise FileChangedError("file shrank on disk")
        if cur[: self.n_snapshot] != self.lines:
            raise FileChangedError("existing lines changed on disk")
        return cur[self.n_snapshot:]

    def save(self) -> int:
        """Apply edits and write. Returns number of lines written."""
        if not self.dirty:
            return self.n_snapshot
        make_backup(self.path, self._source, self._sid)
        tail = self._current_tail() if self._changed_on_disk() else []
        out: list[str] = []
        for i, ln in enumerate(self.lines):
            if i in self._deleted:
                continue
            out.append(self._replaced.get(i, ln))
        out.extend(tail)
        out.extend(self._appended)
        tmp = self.path.with_suffix(".jsonl.admtmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.writelines(out)
        os.replace(tmp, self.path)
        self.lines = out
        self.n_snapshot = len(out)
        self._deleted.clear()
        self._replaced.clear()
        self._appended.clear()
        self.dirty = False
        self._refresh_stat()
        return len(out)

    # set via note_snapshot
    _source = "claude"
    _sid = ""
    _snap_size = 0
    _snap_mtime = 0

    def note_snapshot(self, source: str, sid: str) -> None:
        self._source = source
        self._sid = sid
        self._refresh_stat()

    def _refresh_stat(self) -> None:
        st = self.path.stat()
        self._snap_size = st.st_size
        self._snap_mtime = st.st_mtime_ns

    def _changed_on_disk(self) -> bool:
        try:
            st = self.path.stat()
        except OSError:
            return True
        return st.st_size != self._snap_size or st.st_mtime_ns != self._snap_mtime
