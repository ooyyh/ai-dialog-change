"""Resume a session in a fresh terminal window at the project cwd."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import util
from .models import SessionMeta


def _shell() -> str:
    for cand in ("pwsh", "powershell"):
        p = shutil.which(cand)
        if p:
            return p
    return "powershell"


def open_terminal(command: str, cwd: str) -> None:
    cwd = util.strip_verbatim(cwd) or str(Path.home())
    if not Path(cwd).is_dir():
        cwd = str(Path.home())
    ps = _shell()
    wt = shutil.which("wt")
    if wt:
        args = ["wt", "new-tab", "--startingDirectory", cwd,
                ps, "-NoExit", "-Command", command]
        subprocess.Popen(args, cwd=cwd)
    else:
        subprocess.Popen(
            ["cmd", "/c", "start", "ADM Session", "/D", cwd,
             ps, "-NoExit", "-Command", command],
            cwd=cwd, shell=False)


def resume(meta: SessionMeta) -> bool:
    """Open the source CLI on this session; False for GUI-only stores."""
    from . import sources
    cmd = sources.resume_command(meta)
    if not cmd:
        return False
    if meta.source in ("gemini", "qwen") and not meta.cwd:
        return False
    open_terminal(cmd, meta.cwd or str(Path.home()))
    return True
