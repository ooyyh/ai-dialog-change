"""Persistent scan cache.

Scanning every Claude/Codex transcript on every launch is expensive (hundreds
of files, some tens of MB).  The metadata we show in the list changes only when
the file changes, so cache it keyed by ``(path, mtime_ns, size)``.

The cache is deliberately plain JSON: it is small, easy to inspect, and never
blocks startup if it is corrupt.  A version bump invalidates old caches.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .models import SessionMeta

CACHE_VERSION = 3
CACHE_FILE = config.APP_DIR / "scan_cache.json"


class ScanCache:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else CACHE_FILE
        self._meta: dict[str, dict[str, dict]] = {}
        self._values: dict[str, dict[str, dict]] = {}
        self._dirty = False
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict) or doc.get("version") != CACHE_VERSION:
            return
        meta = doc.get("meta")
        values = doc.get("values")
        if isinstance(meta, dict):
            self._meta = {str(k): v for k, v in meta.items() if isinstance(v, dict)}
        if isinstance(values, dict):
            self._values = {str(k): v for k, v in values.items() if isinstance(v, dict)}

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            config.ensure_app_dirs()
            tmp = self.path.with_name(self.path.name + ".tmp")
            doc = {"version": CACHE_VERSION, "meta": self._meta,
                   "values": self._values}
            tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass

    # ------------------------------------------------------------------ meta
    def get_meta(self, source: str, path: str, mtime_ns: int,
                 size: int) -> SessionMeta | None:
        ent = self._meta.get(source, {}).get(str(path))
        if not ent or ent.get("mtime_ns") != mtime_ns or ent.get("size") != size:
            return None
        raw = ent.get("meta")
        if not isinstance(raw, dict):
            return None
        try:
            known = {f.name for f in fields(SessionMeta)}
            data = {k: v for k, v in raw.items() if k in known}
            for key in ("created", "updated"):
                val = data.get(key)
                if isinstance(val, str):
                    try:
                        data[key] = datetime.fromisoformat(val)
                    except ValueError:
                        data[key] = None
            return SessionMeta(**data)
        except (TypeError, ValueError):
            return None

    def put_meta(self, source: str, path: str, mtime_ns: int, size: int,
                 meta: SessionMeta) -> None:
        raw = dict(meta.__dict__)
        for key in ("created", "updated"):
            val = raw.get(key)
            raw[key] = val.isoformat() if isinstance(val, datetime) else None
        self._meta.setdefault(source, {})[str(path)] = {
            "mtime_ns": int(mtime_ns), "size": int(size), "meta": raw,
        }
        self._dirty = True

    def prune_meta(self, source: str, valid_paths: set[str]) -> None:
        bucket = self._meta.get(source)
        if not bucket:
            return
        stale = [p for p in bucket if p not in valid_paths]
        for p in stale:
            del bucket[p]
        if stale:
            self._dirty = True

    # ---------------------------------------------------------------- values
    def get_value(self, kind: str, path: str, mtime_ns: int,
                  size: int) -> Any | None:
        ent = self._values.get(kind, {}).get(str(path))
        if not ent or ent.get("mtime_ns") != mtime_ns or ent.get("size") != size:
            return None
        return ent.get("value")

    def put_value(self, kind: str, path: str, mtime_ns: int, size: int,
                  value: Any) -> None:
        self._values.setdefault(kind, {})[str(path)] = {
            "mtime_ns": int(mtime_ns), "size": int(size), "value": value,
        }
        self._dirty = True

    def prune_values(self, kind: str, valid_paths: set[str]) -> None:
        bucket = self._values.get(kind)
        if not bucket:
            return
        stale = [p for p in bucket if p not in valid_paths]
        for p in stale:
            del bucket[p]
        if stale:
            self._dirty = True
