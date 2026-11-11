"""Sidecar metadata (stars / tags / notes / custom titles) that never touches
the original session files. Stored as one JSON file in APPDATA."""
from __future__ import annotations

import json
import threading

from . import config

_LOCK = threading.Lock()
_CACHE: dict | None = None

# meta.json shape: {"<source>:<id>": {"starred": true, "tags": [...], ...}}


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(config.META_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _save(data: dict) -> None:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.META_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(config.META_FILE)


def key(source: str, sid: str) -> str:
    return f"{source}:{sid}"


def get(source: str, sid: str) -> dict:
    with _LOCK:
        return _load().get(key(source, sid), {})


def all_meta() -> dict:
    with _LOCK:
        return dict(_load())


def update(source: str, sid: str, **fields) -> None:
    with _LOCK:
        data = _load()
        k = key(source, sid)
        rec = data.get(k, {})
        rec.update(fields)
        data[k] = rec
        _save(data)


def toggle_star(source: str, sid: str) -> bool:
    cur = get(source, sid).get("starred", False)
    update(source, sid, starred=not cur)
    return not cur


def apply_to(metas: list) -> None:
    """Attach local metadata onto a list of SessionMeta objects (in place)."""
    with _LOCK:
        data = _load()
    for m in metas:
        rec = data.get(key(m.source, m.id), {})
        m.starred = bool(rec.get("starred"))
        m.tags = list(rec.get("tags", []))
        m.note = rec.get("note", "")
        m.custom_title = rec.get("custom_title", "")
