"""Small shared helpers."""
from __future__ import annotations

import ctypes
import json
import re
from datetime import datetime, timezone

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z?$"
)

# tz-aware floor for "no timestamp" — mixing naive datetime.min with the
# aware datetimes parse_iso() returns makes max()/sorted() raise.
MIN_DT = datetime(1, 1, 1, tzinfo=timezone.utc)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    m = _ISO_RE.match(ts.strip())
    if not m:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    y, mo, d, h, mi, s, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro,
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def ts_from_ms(ms: int | float | None) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_dt(dt: datetime | None, with_time: bool = True) -> str:
    if dt is None:
        return ""
    local = dt.astimezone()
    if with_time:
        return local.strftime("%Y-%m-%d %H:%M")
    return local.strftime("%Y-%m-%d")


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_tokens(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def one_line(s: str, width: int = 90) -> str:
    s = " ".join(s.split())
    return s[:width] + ("…" if len(s) > width else "")


def collapse_ws(s: str) -> str:
    """Codex stores terminal-padded text — collapse runs of whitespace."""
    return " ".join(s.split())


def dumps_claude(obj: dict) -> str:
    """Claude Code writes compact JSON with non-escaped unicode."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def dumps_codex(obj: dict) -> str:
    """Codex rollout lines use standard separators."""
    return json.dumps(obj, ensure_ascii=False)


def strip_verbatim(p: str) -> str:
    """Codex stores cwd as NT verbatim paths (\\\\?\\E:\\x) — normalize for display/use."""
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def munged_to_path(munged: str) -> str:
    """Claude project dir 'E--ZProject-foo' -> 'E:\\ZProject\\foo' (best effort)."""
    s = munged
    # drive letter: leading 'X--'
    if len(s) > 3 and s[1:3] == "--":
        s = s[0] + ":\\" + s[3:]
    s = s.replace("-", "\\")
    return s


def path_to_display(p: str) -> str:
    p = strip_verbatim(p)
    parts = [x for x in p.replace("/", "\\").split("\\") if x]
    return parts[-1] if parts else p


_KERNEL32 = None


def pid_alive(pid: int) -> bool:
    """Non-destructive liveness check on Windows (never os.kill — it terminates)."""
    global _KERNEL32
    if pid <= 0:
        return False
    if _KERNEL32 is None:
        _KERNEL32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = _KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if _KERNEL32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        _KERNEL32.CloseHandle(h)
