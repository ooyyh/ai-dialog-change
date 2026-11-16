"""Consciousness upload: export a session as a portable, resumable bundle.

A bundle is one self-contained folder:

    <title>-<id8>/
      session.jsonl    raw transcript (Claude project jsonl / Codex rollout)
      metadata.json    identity, provenance, original Codex sqlite row
      install.sh       Linux / macOS installer (POSIX sh; Python optional)
      install.ps1      Windows PowerShell installer
      install.bat      Windows CMD entry point (calls install.ps1)
      install.py       universal fallback installer (Python 3, stdlib only)
      convert.bat      Windows: move the session into another AI tool
      convert.ps1      Windows PowerShell converter
      convert.sh       Linux / macOS converter (POSIX sh)
      convert.py       universal converter (Python 3, stdlib only)
      restore-claude.* restore the converted session and continue it in Claude
      restore-codex.*  restore the converted session and continue it in Codex
      restore-opencode.* restore the converted session and continue it in OpenCode
      restore-gemini.* restore into DeepSeek CLI (same format as Qwen Code)
      restore-qwen.*   restore into Qwen Code
      restore-goose.*  restore into Goose
      restore-cline.*  restore into Cline (VS Code extension)
      restore-roo.*    restore into Roo Code (VS Code extension)
      restore-json.*   write a neutral transcript for any other AI tool
      restore.bat      Windows CMD menu / entry point
      restore.ps1      Windows PowerShell restore helper
      restore.sh       Linux / macOS restore helper
      transplant/      pre-rendered claude / codex / opencode / gemini /
                       goose / cline / neutral JSON transcripts
      README.md        bilingual instructions
      subagents/       Claude sidechain transcripts (when present)

After installing on the target machine, the session lands in that machine's
CLI session store with a fresh session id and cwd rewritten, so ``claude`` /
``codex`` started in the working folder lists it under ``/resume``.  The
convert.* scripts instead write a *converted* transcript — e.g. a Claude
session transplanted into Codex, OpenCode or a neutral JSON transcript — into
any working folder the user chooses.  The restore-* scripts do the same and
then reopen the target CLI on the new session (``claude --resume``,
``codex resume``, ``opencode --session``).
"""
from __future__ import annotations

import json
import re
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

from . import codex_store, config, local_store, util
from .jsonlio import read_lines
from .models import SessionMeta

BUNDLE_VERSION = 1


def safe_name(s: str, limit: int = 60) -> str:
    s = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", s).strip(" ._")
    s = s[:limit].rstrip(" ._")
    return s or "session"


def claude_project_dir_name(cwd: str) -> str:
    """Claude Code encodes a project cwd by replacing every non-alphanumeric
    character with '-'. Verified against ~/.claude/projects on this machine."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _unique_dir(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while True:
        q = p.with_name(f"{p.name}-{n}")
        if not q.exists():
            return q
        n += 1


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _rewrite_ids(lines: list[str], source: str, sid: str) -> str:
    """Rewrite the session identity while keeping every record intact.
    cwd is left for the installer, which knows the target working folder."""
    out: list[str] = []
    for ln in lines:
        s = ln.rstrip("\r\n")
        if not s.strip():
            continue
        try:
            o = json.loads(s)
        except ValueError:
            out.append(s)
            continue
        if isinstance(o, dict):
            if source == "claude":
                if "sessionId" in o:
                    o["sessionId"] = sid
            elif o.get("type") == "session_meta":
                p = o.get("payload")
                if isinstance(p, dict):
                    if "session_id" in p:
                        p["session_id"] = sid
                    if "id" in p:
                        p["id"] = sid
        out.append(util.dumps_claude(o) if source == "claude"
                   else util.dumps_codex(o))
    return "\n".join(out) + "\n"


def _export_claude(meta: SessionMeta, bundle: Path, sid: str) -> dict:
    text = _rewrite_ids(read_lines(meta.path), "claude", sid)
    (bundle / "session.jsonl").write_text(text, encoding="utf-8", newline="\n")
    extra: dict = {"subagents": []}
    src = Path(meta.path).parent / meta.id / "subagents"
    if src.is_dir():
        dst = bundle / "subagents"
        dst.mkdir()
        for f in sorted(src.glob("*.jsonl")):
            (dst / f.name).write_text(
                _rewrite_ids(read_lines(f), "claude", sid),
                encoding="utf-8", newline="\n")
            extra["subagents"].append(f.name)
    return extra


def _codex_thread_row(sid: str) -> dict | None:
    try:
        con = codex_store._connect(config.CODEX_STATE_DB, readonly=True)
        try:
            row = con.execute("SELECT * FROM threads WHERE id=?", (sid,)).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}
    except Exception:  # noqa: BLE001 — codex sqlite is optional metadata
        return None


def _export_codex(meta: SessionMeta, bundle: Path, sid: str) -> dict:
    text = _rewrite_ids(read_lines(meta.path), "codex", sid)
    (bundle / "session.jsonl").write_text(text, encoding="utf-8", newline="\n")
    return {"subagents": [], "codex_thread": _codex_thread_row(meta.id)}


def _export_transplant(meta: SessionMeta, bundle: Path) -> dict | None:
    """Pre-render this session for every other AI tool, so the exported
    scripts can transplant it on a machine that has no Python/app installed."""
    from . import transplant

    try:
        session = transplant.load_source(meta)
        messages = transplant.build_transcript(
            session.messages, transplant.TransplantOptions())
    except Exception:  # noqa: BLE001 — transplant is a bonus, never block export
        return None
    if not messages:
        return None

    title = _one_line(meta.display_title, 200) or "transplanted session"
    ph = transplant.CWD_PLACEHOLDER
    out = bundle / "transplant"
    out.mkdir(parents=True)

    claude_id, claude_text = transplant.build_claude_session(
        messages, title, ph, model=meta.model or "")
    (out / "claude.jsonl").write_text(claude_text, encoding="utf-8", newline="\n")

    codex_id, codex_name, codex_text = transplant.build_codex_rollout(
        messages, title, ph, model=meta.model or "")
    (out / "codex.jsonl").write_text(codex_text, encoding="utf-8", newline="\n")

    agent, model = transplant._opencode_defaults()
    payload = transplant.build_opencode_payload(
        messages, title, ph, agent=agent, model=model)
    (out / "opencode.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="\n")

    gem_id = str(uuidlib.uuid4())
    gem = transplant.build_gemini_session(
        messages, title, ph, model=meta.model or "", sid=gem_id)
    (out / "gemini.json").write_text(
        json.dumps(gem, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    goose = transplant.build_goose_session(messages, title, ph)
    (out / "goose.json").write_text(
        json.dumps(goose, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    cline_id, cline_hist, cline_ui, cline_meta = \
        transplant.build_cline_task(messages, title, ph)
    cline_dir = out / "cline"
    cline_dir.mkdir()
    (cline_dir / "api_conversation_history.json").write_text(
        json.dumps(cline_hist, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (cline_dir / "ui_messages.json").write_text(
        json.dumps(cline_ui, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (cline_dir / "task_metadata.json").write_text(
        json.dumps(cline_meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    neutral = out / "neutral"
    neutral.mkdir()
    doc, md, readme = transplant.build_generic_transcript(
        messages, title, ph, meta.source, meta.source_badge)
    (neutral / "transcript.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (neutral / "transcript.md").write_text(md, encoding="utf-8", newline="\n")
    (neutral / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    return {
        "enabled": True,
        "n_messages": len(messages),
        "cwd_placeholder": ph,
        "claude": {"id": claude_id, "file": "transplant/claude.jsonl"},
        "codex": {"id": codex_id, "file": "transplant/codex.jsonl",
                  "name": codex_name},
        "opencode": {"id": payload["info"]["id"],
                     "file": "transplant/opencode.json"},
        "gemini": {"id": gem_id, "file": "transplant/gemini.json"},
        "qwen": {"id": gem_id, "file": "transplant/gemini.json"},
        "goose": {"id": goose["id"], "file": "transplant/goose.json"},
        "cline": {"id": cline_id, "dir": "transplant/cline"},
        "roo": {"id": cline_id, "dir": "transplant/cline"},
        "json": {"dir": "transplant/neutral"},
    }


def export_session(meta: SessionMeta, dest_root: Path,
                   *, new_id: str | None = None) -> Path:
    """Write one portable bundle under dest_root and return its folder."""
    if meta.source not in ("claude", "codex"):
        raise ValueError(
            f"portable upload bundles support Claude/Codex only; "
            f"{meta.source} can be transplanted to another AI tool instead")
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    sid = new_id or (str(uuidlib.uuid4()) if meta.source == "claude"
                     else codex_store.new_session_id())
    bundle = _unique_dir(dest_root / f"{safe_name(meta.display_title)}-{sid[:8]}")
    bundle.mkdir(parents=True)

    extra: dict = {}
    if meta.source == "claude":
        extra = _export_claude(meta, bundle, sid)
    else:
        extra = _export_codex(meta, bundle, sid)

    local = local_store.get(meta.source, meta.id)
    transplant_meta = _export_transplant(meta, bundle)
    metadata = {
        "bundle_version": BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": meta.source,
        "source_name": "Claude Code" if meta.source == "claude" else "Codex",
        "new_id": sid,
        "original_id": meta.id,
        "title": meta.display_title,
        "original_title": meta.title,
        "project": meta.project,
        "original_cwd": meta.cwd,
        "created": _iso(meta.created),
        "updated": _iso(meta.updated),
        "n_messages": meta.n_messages,
        "n_user": meta.n_user,
        "n_assistant": meta.n_assistant,
        "size": meta.size,
        "model": meta.model,
        "tokens": meta.tokens,
        "preview": meta.preview,
        "transcript": "session.jsonl",
        "local": {
            "starred": if_get(local, "starred", False),
            "tags": if_get(local, "tags", []),
            "note": if_get(local, "note", ""),
            "custom_title": if_get(local, "custom_title", ""),
        },
        **extra,
    }
    if transplant_meta:
        metadata["transplant"] = transplant_meta
    (bundle / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    (bundle / "install.py").write_text(INSTALL_PY, encoding="utf-8", newline="\n")
    (bundle / "install.ps1").write_text(INSTALL_PS1, encoding="utf-8", newline="\n")
    (bundle / "install.bat").write_text(INSTALL_BAT, encoding="utf-8", newline="\r\n")
    (bundle / "install.sh").write_text(
        _fill(INSTALL_SH, {
            "SOURCE": _sh_quote(meta.source),
            "NEW_ID": _sh_quote(sid),
            "ORIG_CWD_JSON": _sh_quote(_json_escaped(meta.cwd or "")),
            "TITLE": _sh_quote(_one_line(meta.display_title, 300)),
            "FIRST_USER": _sh_quote(_one_line(meta.preview, 500)),
            "PREVIEW": _sh_quote(_one_line(meta.preview, 500)),
            "MODEL": _sh_quote(_one_line(meta.model or "custom", 120)),
            "TOKENS": str(meta.tokens or 0),
            "TRANSCRIPT": _sh_quote("session.jsonl"),
        }),
        encoding="utf-8", newline="\n")
    (bundle / "README.md").write_text(
        _readme(metadata), encoding="utf-8", newline="\n")
    if transplant_meta:
        (bundle / "convert.ps1").write_text(
            CONVERT_PS1, encoding="utf-8", newline="\n")
        (bundle / "convert.bat").write_text(
            CONVERT_BAT, encoding="utf-8", newline="\r\n")
        (bundle / "convert.sh").write_text(
            CONVERT_SH, encoding="utf-8", newline="\n")
        (bundle / "convert.py").write_text(
            CONVERT_PY, encoding="utf-8", newline="\n")
        _write_restore_scripts(bundle)
    return bundle


# One-click "restore into X and continue" wrappers.  They reuse convert.*
# for the actual transplant, then relaunch the target CLI on the new id.
RESTORE_TARGETS = (
    ("claude", "Claude Code"),
    ("codex", "Codex"),
    ("opencode", "OpenCode"),
    ("gemini", "DeepSeek CLI"),
    ("qwen", "Qwen Code"),
    ("goose", "Goose"),
    ("cline", "Cline (VS Code)"),
    ("roo", "Roo Code (VS Code)"),
    ("json", "generic JSON / Markdown"),
)


def _write_restore_scripts(bundle: Path) -> None:
    (bundle / "restore.ps1").write_text(
        RESTORE_PS1, encoding="utf-8", newline="\n")
    (bundle / "restore.bat").write_text(
        RESTORE_BAT, encoding="utf-8", newline="\r\n")
    (bundle / "restore.sh").write_text(
        RESTORE_SH, encoding="utf-8", newline="\n")
    for target, label in RESTORE_TARGETS:
        for ext in ("bat", "ps1", "sh"):
            template = {
                "bat": RESTORE_TARGET_BAT,
                "ps1": RESTORE_TARGET_PS1,
                "sh": RESTORE_TARGET_SH,
            }[ext]
            text = (template.replace("__TARGET__", target)
                    .replace("__LABEL__", label))
            newline = "\r\n" if ext == "bat" else "\n"
            (bundle / f"restore-{target}.{ext}").write_text(
                text, encoding="utf-8", newline=newline)


def _one_line(s: str, limit: int) -> str:
    return " ".join(str(s or "").split())[:limit]


def _json_escaped(s: str) -> str:
    """The path as it appears between the quotes of a compact JSON string."""
    return json.dumps(s or "", ensure_ascii=False)[1:-1]


def _sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def _fill(template: str, values: dict) -> str:
    for key, val in values.items():
        template = template.replace(f"__{key}__", val)
    return template


def if_get(d: dict, key: str, default):
    v = d.get(key)
    return default if v is None else v


def _readme(md: dict) -> str:
    sid = md["new_id"]
    source = md["source"]
    name = md["source_name"]
    title = " ".join(str(md["title"]).split())[:120]
    if source == "claude":
        manual = (
            "1. 项目目录名 = 工作文件夹路径中每个非字母数字字符替换为 `-`\n"
            "   (例:`E:\\work\\demo` → `E--work-demo`)\n"
            f"2. 把 `session.jsonl` 复制到 `~/.claude/projects/<项目目录名>/{sid}.jsonl`\n"
            "3. 若有 `subagents/`,复制到 "
            f"`~/.claude/projects/<项目目录名>/{sid}/subagents/`\n"
            "4. 在工作文件夹运行 `claude`,输入 `/resume`\n"
        )
        manual_en = (
            "1. Project dir name = working-folder path with every non-alphanumeric\n"
            "   character replaced by `-` (e.g. `E:\\work\\demo` → `E--work-demo`)\n"
            f"2. Copy `session.jsonl` to `~/.claude/projects/<project-dir>/{sid}.jsonl`\n"
            "3. If `subagents/` exists, copy it to "
            f"`~/.claude/projects/<project-dir>/{sid}/subagents/`\n"
            "4. Run `claude` in the working folder and type `/resume`\n"
        )
    else:
        manual = (
            "1. 把 `session.jsonl` 复制到 "
            "`~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<id>.jsonl`\n"
            "2. 在 `~/.codex/state_*.sqlite` 的 `threads` 表插入一行\n"
            f"   (id=`{sid}`, rollout_path=上面的文件, cwd=目标工作文件夹)\n"
            "   —— 推荐直接用 `install.py` / `install.sh` / `install.ps1`,\n"
            "   它们会自动完成这一步(没有 Python 时用 sqlite3)\n"
            "3. 在工作文件夹运行 `codex`,输入 `/resume`\n"
        )
        manual_en = (
            "1. Copy `session.jsonl` to "
            "`~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl`\n"
            "2. Insert a `threads` row in `~/.codex/state_*.sqlite`\n"
            f"   (id=`{sid}`, rollout_path=that file, cwd=target working folder)\n"
            "   — easiest: let `install.py` / `install.sh` / `install.ps1` do it\n"
            "   (sqlite3 is used when Python is absent)\n"
            "3. Run `codex` in the working folder and type `/resume`\n"
        )
    transplant_block = ""
    if md.get("transplant"):
        tp = md["transplant"]
        transplant_block = f"""
## 换一个 AI 工具 / Move it to another AI tool

包内已预渲染好各目标的会话记录(`transplant/`),目标机器不需要装本应用。
Every target transcript is pre-rendered in `transplant/` — no app needed here.

### 一键恢复并继续 / One-click restore and continue

```bat
:: Windows — 转换 + 直接打开目标工具的新会话
restore-codex.bat "D:\\hello"
restore-opencode.bat "D:\\hello"
restore-claude.bat "D:\\hello"
restore-gemini.bat "D:\\hello"
restore-qwen.bat "D:\\hello"
restore-goose.bat "D:\\hello"
restore-cline.bat "D:\\hello"
restore-roo.bat "D:\\hello"
restore-json.bat "D:\\hello"
restore.bat
```

```sh
# Linux / macOS — 转换 + 直接进入目标工具的新会话
sh restore-codex.sh "/home/user/hello"
sh restore-opencode.sh "/home/user/hello"
sh restore-claude.sh "/home/user/hello"
sh restore-gemini.sh "/home/user/hello"
sh restore-qwen.sh "/home/user/hello"
sh restore-goose.sh "/home/user/hello"
sh restore-json.sh "/home/user/hello"
```

- `restore-codex` / `restore-opencode` / `restore-claude` 会转换记录,然后分别执行
  `codex resume <id>` / `opencode --session <id>` / `claude --resume <id>`。
- `restore-gemini` / `restore-qwen` / `restore-goose` 分别执行
  `gemini --resume <id>` / `qwen --resume <id>` / `goose session --resume --name <id>`。
- `restore-cline` / `restore-roo` 只安装到 VS Code 扩展的任务目录,然后在历史列表里打开。
- `restore-json` 只写出通用 `transcript.json` + `transcript.md`,交给任意其它 AI 工具。
- 新增目标(gemini/qwen/goose/cline/roo)在 Windows 的 `convert.ps1` 下需要 Python 3;
  Linux/macOS 的 `convert.sh` 同样优先调用 `convert.py`。
- 不想自动打开时加 `--no-launch`(PowerShell 为 `-NoLaunch`),只安装并打印命令。
- 重复安装同一个包时加 `--force`(PowerShell 为 `-Force`),覆盖包内固定的目标 id。
- 高级用户仍可直接用 `convert.*`:

```bat
:: Windows
convert.bat codex "D:\\hello"
convert.bat claude "D:\\hello"
convert.bat gemini "D:\\hello"
convert.bat goose "D:\\hello"
```

```sh
# Linux / macOS
sh convert.sh codex "/home/user/hello"
sh convert.sh json "/home/user/hello"
sh convert.sh cline "/home/user/hello"
```

| 目标 / target | 结果 / result | 继续 / continue |
|---|---|---|
| `claude` | `~/.claude/projects/<转义目录>/<id>.jsonl` | `claude --resume <id>` |
| `codex` | `~/.codex/sessions/年/月/日/rollout-*.jsonl` + sqlite 登记 | `codex resume <id>` |
| `opencode` | 通过本机 OpenCode 服务导入(POST `/api/experimental/session/import`) | `opencode --session <id>` |
| `gemini` | `~/.gemini/tmp/<sha256(目录)>/chats/session-*.json` | `gemini --resume <id>` |
| `qwen` | `~/.qwen/tmp/<sha256(目录)>/chats/session-*.json` | `qwen --resume <id>` |
| `goose` | `~/.local/share/goose/sessions/<id>.json`(Windows `%APPDATA%\\Block\\goose\\data\\sessions`) | `goose session --resume --name <id>` |
| `cline` | VS Code `globalStorage/saoudrizwan.claude-dev/tasks/<id>/` | 打开 VS Code → Cline → 历史 |
| `roo` | VS Code `globalStorage/rooveterinaryinc.roo-cline/tasks/<id>/` | 打开 VS Code → Roo Code → 历史 |
| `json` | `<目录>/<标题>-<id8>/transcript.json` + `.md` + `README.md` | 交给任意工具/代理 |

- 目标工作目录可以和原始目录完全不同(例如原 `E:\\test` → 新 `D:\\hello`)。
- 工具调用/结果已转成可读文本:目标工具不认识源工具的 tool schema,原样保留会导致恢复后请求报错。
- 转换按源消息的文本/思考/工具调用/工具结果拆分;一次工具调用和它的结果放在同一条,不会把整段对话合并成一个巨大消息块。
- 转换后新 id:`claude` `{tp["claude"]["id"]}` · `codex` `{tp["codex"]["id"]}` · `opencode` `{tp["opencode"]["id"]}` · `gemini/qwen` `{tp["gemini"]["id"]}` · `goose` `{tp["goose"]["id"]}` · `cline/roo` `{tp["cline"]["id"]}`
- 选项:`--dry-run` / `--force`(ps1 为 `-DryRun` / `-Force`),Codex 可用 `CODEX_HOME` / `CODEX_STATE_DB` 覆盖;
  新目标可用 `GEMINI_CLI_HOME` / `QWEN_HOME` / `GOOSE_HOME`,或用 `convert.py --gemini-home/--qwen-home/--goose-home/--vscode-storage`。

换工具也可以不用脚本,直接在 App 里选中会话 → 工具栏「⤴ 适配其它 AI」。
"""
    return f"""# 意识上传包 · {title}

- 来源 / source: **{name}**
- 新会话 id / new session id: `{sid}`
- 原始 id / original id: `{md["original_id"]}`
- 原始工作目录 / original cwd: `{md["original_cwd"] or "–"}`
- 消息数 / messages: {md["n_messages"]} · 大小 / size: {util.fmt_size(md["size"])}
- 导出时间 / exported: {md["created_at"]}

这是一个完整的可恢复会话包 / a complete, resumable session bundle。

## 一键安装 / One-command install

Windows (CMD) — 双击也可以 / double-click also works:

```bat
install.bat "D:\\work\\project"
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Cwd "D:\\work\\project"
```

Linux / macOS:

```sh
sh install.sh "/home/user/project"
```

Python 3 通用备用 / universal fallback:

```bash
python install.py --cwd "/path/to/project"
```

不指定工作文件夹时默认当前目录 / the working folder defaults to the current directory.
Claude 的 `install.sh` / `install.ps1` 不需要 Python;Codex 的线程注册
需要 Python 或 `sqlite3`(都没有时会打印可直接执行的 SQL)。
The Claude path needs no Python; the Codex thread registration uses Python or
`sqlite3` (the exact SQL is printed when neither is available).

安装完成后 / afterwards:

```bash
cd "<目标工作文件夹>" && {("claude" if md["source"] == "claude" else "codex")}
/resume
```

可选参数 / options:

```text
install.ps1  -Id ID  -ClaudeHome DIR  -CodexHome DIR  -StateDb FILE
             -DryRun  -Force
install.sh   --dry-run  --force
             env: CLAUDE_CONFIG_DIR, CODEX_HOME, CODEX_STATE_DB
install.py   --id ID  --claude-home DIR  --codex-home DIR  --state-db FILE
             --dry-run  --force
```
{transplant_block}
## 手动安装 / Manual install

### {name}

中文:
{manual}
English:
{manual_en}
## 包里有什么 / What is inside

- `session.jsonl` — 完整原始记录(会话 id 已换成新 id)
- `metadata.json` — 身份信息、原始 id、来源、本地收藏/备注、Codex sqlite 原始行
- `install.bat` / `install.ps1` — Windows 安装器(同工具恢复)
- `install.sh` — Linux / macOS 安装器
- `install.py` — 通用备用安装器(Python 3,无第三方依赖)
- `convert.bat` / `convert.ps1` / `convert.sh` / `convert.py` — 换 AI 工具的转换器
- `restore-claude.*` / `restore-codex.*` / `restore-opencode.*`
  / `restore-gemini.*` / `restore-qwen.*` / `restore-goose.*`
  / `restore-cline.*` / `restore-roo.*` / `restore-json.*`
  — 一键“转换 + 继续”脚本(`.bat` / `.ps1` / `.sh`)
- `restore.bat` / `restore.ps1` / `restore.sh` — 带菜单的统一恢复入口
- `transplant/` — 预渲染的 claude / codex / opencode / 中立 JSON 记录,
  外加 `gemini.json`、`goose.json`、`cline/`(DeepSeek / Qwen / Goose / Cline 共用)
- `subagents/` — Claude 子代理记录(如有)

原始会话不会被修改 / the source session is never modified.
"""


# The installer is written into every bundle. Stdlib only, Python 3.8+.
INSTALL_PY = r'''#!/usr/bin/env python3
"""Install an AI Dialog Manager "consciousness upload" session bundle.

Usage:
    python install.py [--cwd DIR] [--id SESSION_ID] [--dry-run] [--force]
                      [--claude-home DIR] [--codex-home DIR] [--state-db FILE]

Copies the bundled transcript into the target machine's Claude Code / Codex
session store, rewrites the session id and working directory, and (for Codex)
registers the thread in state_*.sqlite. Afterwards start the CLI in the working
folder and use /resume.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def munge(path: str) -> str:
    """Claude Code project-dir encoding: every non-alphanumeric -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def load_meta() -> dict:
    p = HERE / "metadata.json"
    if not p.exists():
        sys.exit(f"metadata.json not found next to install.py ({HERE})")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _rewrite_obj(v, source: str, sid: str, cwd: str) -> None:
    if isinstance(v, dict):
        for k in list(v.keys()):
            if k == "cwd" and isinstance(v[k], str):
                v[k] = cwd
            elif k == "workingDirectory" and isinstance(v[k], str):
                v[k] = cwd
            elif k == "workspace_roots" and isinstance(v[k], list):
                v[k] = [cwd]
            else:
                _rewrite_obj(v[k], source, sid, cwd)
        if source == "claude" and "sessionId" in v:
            v["sessionId"] = sid
        if source == "codex" and v.get("type") == "session_meta":
            p = v.get("payload")
            if isinstance(p, dict):
                p["cwd"] = cwd
                if "session_id" in p:
                    p["session_id"] = sid
                if "id" in p:
                    p["id"] = sid
    elif isinstance(v, list):
        for x in v:
            _rewrite_obj(x, source, sid, cwd)


def rewrite_text(text: str, source: str, sid: str, cwd: str) -> str:
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        _rewrite_obj(o, source, sid, cwd)
        out.append(json.dumps(o, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(out) + "\n"


def write_file(path: Path, text: str, dry: bool, force: bool) -> None:
    if path.exists() and not force:
        sys.exit(f"refusing to overwrite: {path}\n(use --force to replace it)")
    print(("DRY-RUN would write " if dry else "write ") + str(path))
    if dry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def claude_home(args) -> Path:
    if args.claude_home:
        return Path(args.claude_home).expanduser()
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def codex_home(args) -> Path:
    if args.codex_home:
        return Path(args.codex_home).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def install_claude(meta: dict, cwd: str, sid: str, args) -> None:
    root = claude_home(args)
    project = root / "projects" / munge(cwd)
    src = HERE / meta.get("transcript", "session.jsonl")
    dst = project / f"{sid}.jsonl"
    write_file(dst, rewrite_text(src.read_text(encoding="utf-8", errors="replace"),
                                 "claude", sid, cwd), args.dry_run, args.force)
    sub = HERE / "subagents"
    if sub.is_dir():
        for f in sorted(sub.glob("*.jsonl")):
            out = project / sid / "subagents" / f.name
            write_file(out,
                       rewrite_text(f.read_text(encoding="utf-8", errors="replace"),
                                    "claude", sid, cwd),
                       args.dry_run, args.force)
    print(f"\nClaude session '{sid}' "
          f"{'would be installed' if args.dry_run else 'installed'} "
          f"for working folder:\n  {cwd}")
    print("Afterwards run:  claude   ->  /resume")


def find_state_db(root: Path, override: str | None) -> Path | None:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("CODEX_STATE_DB")
    if env:
        return Path(env).expanduser()
    p = root / "state_5.sqlite"
    if p.exists():
        return p
    cands = sorted(root.glob("state_*.sqlite"),
                   key=lambda x: int(re.sub(r"\D", "", x.stem) or 0), reverse=True)
    return cands[0] if cands else None


def register_thread(db: Path, meta: dict, sid: str, cwd: str, rollout: Path,
                    dry: bool, force: bool) -> bool:
    try:
        con = sqlite3.connect(str(db), timeout=8)
    except sqlite3.Error as e:
        print(f"warning: cannot open {db}: {e}")
        return False
    try:
        info = list(con.execute("PRAGMA table_info(threads)"))
        if not info:
            print(f"warning: {db} has no threads table")
            return False
        cols = [r[1] for r in info]
        required = {r[1]: (r[2] or "", r[3], r[4]) for r in info}
        row = dict(meta.get("codex_thread") or {})
        now = int(time.time())
        now_ms = int(time.time() * 1000)
        title = meta.get("title") or row.get("title") or ""
        vals = dict(row)
        vals.update({
            "id": sid,
            "rollout_path": str(rollout),
            "cwd": cwd,
            "title": title,
            "archived": 0,
            "archived_at": None,
            "created_at": now,
            "updated_at": now,
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "recency_at": now,
            "recency_at_ms": now_ms,
            "is_pinned": 0,
            "first_user_message": row.get("first_user_message")
            or meta.get("preview") or title,
            "preview": row.get("preview") or meta.get("preview") or title,
            "thread_section_id": None,
            "project_id": None,
            "section_position": None,
            "section_entered_at_ms": None,
            "attached_thread_parent_id": None,
            "source": row.get("source") or "cli",
            "model_provider": row.get("model_provider")
            or meta.get("model") or "custom",
            "sandbox_policy": row.get("sandbox_policy") or "danger-full-access",
            "approval_mode": row.get("approval_mode") or "never",
        })
        use = [c for c in cols if c in vals and vals[c] is not None]
        for name, (typ, notnull, dflt) in required.items():
            if name in use or not notnull or dflt is not None:
                continue
            typ = typ.upper()
            vals[name] = "" if ("CHAR" in typ or "TEXT" in typ) else 0
            use.append(name)
        use = [c for c in cols if c in use]
        if con.execute("SELECT 1 FROM threads WHERE id=?", (sid,)).fetchone() \
                and not force:
            print(f"warning: thread {sid} already exists in {db} "
                  f"(use --force to replace)")
            return False
        if dry:
            print(f"DRY-RUN would insert thread {sid} into {db}")
            return True
        q = (f"INSERT OR REPLACE INTO threads ({','.join(use)}) "
             f"VALUES ({','.join('?' * len(use))})")
        con.execute(q, [vals[c] for c in use])
        con.commit()
        print(f"registered thread {sid} in {db}")
        return True
    except sqlite3.Error as e:
        print(f"warning: could not register thread: {e}")
        return False
    finally:
        con.close()


def install_codex(meta: dict, cwd: str, sid: str, args) -> None:
    root = codex_home(args)
    now = datetime.now()
    rollout = (root / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
               / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl")
    src = HERE / meta.get("transcript", "session.jsonl")
    write_file(rollout,
               rewrite_text(src.read_text(encoding="utf-8", errors="replace"),
                            "codex", sid, cwd), args.dry_run, args.force)
    db = find_state_db(root, args.state_db)
    if db and db.exists():
        register_thread(db, meta, sid, cwd, rollout, args.dry_run, args.force)
    else:
        print(f"warning: no Codex state DB under {root}; copied the rollout only")
    print(f"\nCodex session '{sid}' "
          f"{'would be installed' if args.dry_run else 'installed'} "
          f"for working folder:\n  {cwd}")
    print("Afterwards run:  codex   ->  /resume")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install an AI Dialog Manager session bundle")
    ap.add_argument("--cwd", default=os.getcwd(),
                    help="target working folder (default: current directory)")
    ap.add_argument("--id", default=None, help="override the new session id")
    ap.add_argument("--claude-home", default=None)
    ap.add_argument("--codex-home", default=None)
    ap.add_argument("--state-db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    meta = load_meta()
    cwd = os.path.abspath(os.path.expanduser(args.cwd))
    sid = args.id or meta["new_id"]
    source = meta.get("source")
    if source == "claude":
        install_claude(meta, cwd, sid, args)
    elif source == "codex":
        install_codex(meta, cwd, sid, args)
    else:
        sys.exit(f"unknown source in metadata.json: {source!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# Windows PowerShell installer. ASCII only on purpose: Windows PowerShell 5.1
# decodes BOM-less scripts as the system ANSI code page, so non-ASCII here
# would be mangled. All user-visible values are read from metadata.json as
# UTF-8 at run time.
INSTALL_PS1 = r'''# Install an AI Dialog Manager "consciousness upload" session bundle.
# Usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Cwd DIR]
#       [-ClaudeHome DIR] [-CodexHome DIR] [-StateDb FILE] [-Id ID]
#       [-DryRun] [-Force]
# The working folder defaults to the current directory.
param(
    [string]$Cwd = "",
    [string]$ClaudeHome = "",
    [string]$CodexHome = "",
    [string]$StateDb = "",
    [string]$Id = "",
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
if (-not $Here) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
$MetaPath = Join-Path $Here "metadata.json"
if (-not (Test-Path -LiteralPath $MetaPath)) {
    throw "metadata.json not found next to install.ps1 ($Here)"
}
$meta = Get-Content -LiteralPath $MetaPath -Raw -Encoding UTF8 | ConvertFrom-Json
$source = [string]$meta.source
$transcript = [string]$meta.transcript
if (-not $transcript) { $transcript = "session.jsonl" }
$newId = [string]$meta.new_id
$sid = if ($Id) { $Id } else { $newId }
$originalCwd = [string]$meta.original_cwd
$title = (([string]$meta.title) -replace "\s+", " ").Trim()
if ($title.Length -gt 300) { $title = $title.Substring(0, 300) }
$preview = (([string]$meta.preview) -replace "\s+", " ").Trim()
if ($preview.Length -gt 500) { $preview = $preview.Substring(0, 500) }
$model = (([string]$meta.model) -replace "\s+", " ").Trim()
if (-not $model) { $model = "custom" }
$tokens = 0L
if ($meta.tokens) { $tokens = [long]$meta.tokens }

if ($Cwd) {
    $work = [System.IO.Path]::GetFullPath($Cwd)
} else {
    $work = [System.IO.Path]::GetFullPath((Get-Location).Path)
}
if (-not (Test-Path -LiteralPath $work)) {
    throw "working folder not found: $work"
}
$trimmed = $work.TrimEnd([char[]]@("\", "/"))
if ($trimmed -match "^[A-Za-z]:$") { $work = $trimmed + "\" } else { $work = $trimmed }

$Utf8 = New-Object System.Text.UTF8Encoding($false)

function JsonPath([string]$p) {
    if ([string]::IsNullOrEmpty($p)) { return "" }
    return '"' + $p.Replace("\", "\\").Replace('"', '\"') + '"'
}

function Rewrite-Text([string]$text) {
    $t = $text
    if ($originalCwd) {
        $old = JsonPath $originalCwd
        $new = JsonPath $work
        if ($old -ne $new) { $t = $t.Replace($old, $new) }
    }
    if ($sid -ne $newId) { $t = $t.Replace($newId, $sid) }
    return $t
}

function Render-Text([string]$path, [string]$cwd) {
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    return $text.Replace($ph, $cwd)
}

function Write-Target([string]$dest, [string]$text) {
    if ((Test-Path -LiteralPath $dest) -and (-not $Force)) {
        throw "refusing to overwrite: $dest (use -Force)"
    }
    if ($DryRun) {
        Write-Host ("DRY-RUN would write " + $dest)
        return
    }
    $dir = Split-Path -Parent $dest
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($dest, $text, $Utf8)
    Write-Host ("write " + $dest)
}

function Install-Claude {
    $homeDir = if ($ClaudeHome) { $ClaudeHome }
        elseif ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR }
        else { Join-Path $HOME ".claude" }
    $munged = $work -replace "[^A-Za-z0-9]", "-"
    $project = Join-Path (Join-Path $homeDir "projects") $munged
    $dest = Join-Path $project ($sid + ".jsonl")
    $src = Join-Path $Here $transcript
    if (-not (Test-Path -LiteralPath $src)) { throw "missing transcript: $src" }
    $text = [System.IO.File]::ReadAllText($src, [System.Text.Encoding]::UTF8)
    Write-Target $dest (Rewrite-Text $text)
    $sub = Join-Path $Here "subagents"
    if (Test-Path -LiteralPath $sub) {
        Get-ChildItem -LiteralPath $sub -Filter "*.jsonl" | ForEach-Object {
            $s = [System.IO.File]::ReadAllText($_.FullName, [System.Text.Encoding]::UTF8)
            $out = Join-Path (Join-Path (Join-Path $project $sid) "subagents") $_.Name
            Write-Target $out (Rewrite-Text $s)
        }
    }
    Write-Host ""
    Write-Host ("Claude session '" + $sid + "' installed for: " + $work)
    Write-Host "Run:  cd `"$work`"; claude   then  /resume"
}

function Find-Python {
    foreach ($name in @("py.exe", "python.exe", "python3.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            & $cmd.Source -c "import sys" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $cmd.Source }
        } catch {
        }
    }
    return $null
}

function SqlQuote([string]$s) {
    if ($null -eq $s) { $s = "" }
    return "'" + $s.Replace("'", "''") + "'"
}

function Install-Codex {
    $homeDir = if ($CodexHome) { $CodexHome }
        elseif ($env:CODEX_HOME) { $env:CODEX_HOME }
        else { Join-Path $HOME ".codex" }
    $installPy = Join-Path $Here "install.py"
    $py = Find-Python
    if ($py -and (Test-Path -LiteralPath $installPy)) {
        $a = @($installPy, "--cwd", $work, "--codex-home", $homeDir)
        if ($StateDb) { $a += @("--state-db", $StateDb) }
        if ($Id) { $a += @("--id", $Id) }
        if ($DryRun) { $a += "--dry-run" }
        if ($Force) { $a += "--force" }
        & $py @a
        exit $LASTEXITCODE
    }
    $now = Get-Date
    $dir = Join-Path (Join-Path (Join-Path (Join-Path $homeDir "sessions") $now.ToString("yyyy")) $now.ToString("MM")) $now.ToString("dd")
    $stamp = $now.ToString("yyyy-MM-ddTHH-mm-ss")
    $dest = Join-Path $dir ("rollout-" + $stamp + "-" + $sid + ".jsonl")
    $src = Join-Path $Here $transcript
    if (-not (Test-Path -LiteralPath $src)) { throw "missing transcript: $src" }
    $text = [System.IO.File]::ReadAllText($src, [System.Text.Encoding]::UTF8)
    Write-Target $dest (Rewrite-Text $text)

    $db = $StateDb
    if (-not $db) {
        $p5 = Join-Path $homeDir "state_5.sqlite"
        if (Test-Path -LiteralPath $p5) {
            $db = $p5
        } else {
            $c = Get-ChildItem -LiteralPath $homeDir -Filter "state_*.sqlite" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -First 1
            if ($c) { $db = $c.FullName } else { $db = $p5 }
        }
    }
    $nowSec = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $sql = "INSERT OR REPLACE INTO threads " +
        "(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, " +
        "sandbox_policy, approval_mode, tokens_used, has_user_event, archived, first_user_message, preview) VALUES (" +
        (SqlQuote $sid) + ", " + (SqlQuote $dest) + ", " + $nowSec + ", " + $nowSec + ", 'cli', " +
        (SqlQuote $model) + ", " + (SqlQuote $work) + ", " + (SqlQuote $title) + ", " +
        "'danger-full-access', 'never', " + $tokens + ", 0, 0, " + (SqlQuote $preview) + ", " + (SqlQuote $preview) + ");"
    $sqlite = Get-Command "sqlite3.exe" -ErrorAction SilentlyContinue
    if (-not $sqlite) { $sqlite = Get-Command "sqlite3" -ErrorAction SilentlyContinue }
    if ($sqlite -and (Test-Path -LiteralPath $db)) {
        if ($DryRun) {
            Write-Host ("DRY-RUN would run against " + $db + ":")
            Write-Host $sql
        } else {
            try {
                $sql | & $sqlite.Source $db
                if ($LASTEXITCODE -eq 0) {
                    Write-Host ("registered thread " + $sid + " in " + $db)
                } else {
                    Write-Host "warning: sqlite3 failed to register the thread; run this SQL manually:"
                    Write-Host $sql
                }
            } catch {
                Write-Host ("warning: sqlite3 failed: " + $_.Exception.Message)
                Write-Host $sql
            }
        }
    } else {
        Write-Host "warning: sqlite3 not found (or no state DB); the rollout is copied but not registered."
        Write-Host ("Run this SQL in " + $db + " :")
        Write-Host $sql
    }
    Write-Host ""
    Write-Host ("Codex session '" + $sid + "' installed for: " + $work)
    Write-Host "Run:  cd `"$work`"; codex   then  /resume"
}

switch ($source) {
    "claude" { Install-Claude }
    "codex"  { Install-Codex }
    default  { throw "unknown source in metadata.json: $source" }
}
'''


# Windows CMD entry point: double-click support plus a direct folder argument.
INSTALL_BAT = r'''@echo off
rem Install an AI Dialog Manager "consciousness upload" session bundle.
rem Usage: install.bat ["target working folder"]
setlocal
set "HERE=%~dp0"
if not "%~1"=="" goto :with_arg

echo Installing uploaded session bundle...
set "WORK=%CD%"
set /p "WORK=Target working folder [%CD%]: "
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install.ps1" -Cwd "%WORK%"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Install failed with exit code %RC%.
pause
exit /b %RC%

:with_arg
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install.ps1" -Cwd "%~1"
exit /b %ERRORLEVEL%
'''


# Linux / macOS installer. Baked values are shell-quoted at export time.
# Claude needs only POSIX tools (perl/python optional for cwd rewriting);
# Codex delegates to install.py when Python exists, else uses sqlite3, else
# prints the exact SQL row to run manually.
INSTALL_SH = r'''#!/bin/sh
# Install an AI Dialog Manager "consciousness upload" session bundle.
# Usage: sh install.sh [WORKDIR] [--dry-run] [--force]
#   WORKDIR defaults to the current directory.
set -u

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SOURCE=__SOURCE__
NEW_ID=__NEW_ID__
ORIG_CWD_JSON=__ORIG_CWD_JSON__
TITLE=__TITLE__
FIRST_USER=__FIRST_USER__
PREVIEW=__PREVIEW__
MODEL=__MODEL__
TOKENS=__TOKENS__
TRANSCRIPT=__TRANSCRIPT__

CWD="$PWD"
DRY=0
FORCE=0

usage() {
    echo "usage: sh install.sh [WORKDIR] [--dry-run] [--force]"
    echo "  WORKDIR defaults to the current directory"
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) CWD="$arg" ;;
    esac
done

if [ ! -d "$CWD" ]; then
    echo "working folder not found: $CWD" >&2
    exit 1
fi
CWD=$(CDPATH= cd -- "$CWD" && pwd -P) || exit 1

claude_home() {
    if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
        printf '%s' "$CLAUDE_CONFIG_DIR"
    else
        printf '%s/.claude' "${HOME:-$CWD}"
    fi
}

codex_home() {
    if [ -n "${CODEX_HOME:-}" ]; then
        printf '%s' "$CODEX_HOME"
    else
        printf '%s/.codex' "${HOME:-$CWD}"
    fi
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# `python` may be the Windows Store stub, which fails on any -c invocation
find_python() {
    for cand in python3 python; do
        p=$(command -v "$cand" 2>/dev/null) || continue
        if "$p" -c "import sys" >/dev/null 2>&1; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

rewrite_file() {
    src=$1
    dst=$2
    mkdir -p "$(dirname "$dst")" || return 1
    cp "$src" "$dst" || return 1
    [ -n "$ORIG_CWD_JSON" ] || return 0
    new_json=$(json_escape "$CWD")
    [ "$ORIG_CWD_JSON" = "$new_json" ] && return 0
    if command -v perl >/dev/null 2>&1; then
        OLD="$ORIG_CWD_JSON" NEW="$new_json" perl -0pi \
            -e 'my $r = quotemeta($ENV{OLD}); s/$r/$ENV{NEW}/g' "$dst"
    else
        py=$(find_python || true)
        if [ -n "$py" ]; then
            "$py" - "$dst" "$ORIG_CWD_JSON" "$new_json" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as f:
    text = f.read()
with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text.replace(old, new))
PYEOF
        else
            echo "note: perl/python not found; cwd left unchanged (session still resumes)"
        fi
    fi
}

find_state_db() {
    home=$1
    if [ -n "${CODEX_STATE_DB:-}" ] && [ -f "$CODEX_STATE_DB" ]; then
        printf '%s' "$CODEX_STATE_DB"
        return 0
    fi
    if [ -f "$home/state_5.sqlite" ]; then
        printf '%s' "$home/state_5.sqlite"
        return 0
    fi
    for f in "$home"/state_*.sqlite; do
        [ -e "$f" ] && printf '%s\n' "$f"
    done | sort | tail -n 1
}

sql_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"
}

codex_sql() {
    rollout=$1
    now_s=$(date +%s)
    printf "INSERT OR REPLACE INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event, archived, first_user_message, preview) VALUES (%s, %s, %s, %s, 'cli', %s, %s, %s, 'danger-full-access', 'never', %s, 0, 0, %s, %s);\n" \
        "$(sql_quote "$NEW_ID")" "$(sql_quote "$rollout")" "$now_s" "$now_s" \
        "$(sql_quote "$MODEL")" "$(sql_quote "$CWD")" "$(sql_quote "$TITLE")" \
        "$TOKENS" "$(sql_quote "$FIRST_USER")" "$(sql_quote "$PREVIEW")"
}

install_claude() {
    home=$(claude_home)
    munged=$(printf '%s' "$CWD" | sed 's/[^A-Za-z0-9]/-/g')
    project="$home/projects/$munged"
    dst="$project/$NEW_ID.jsonl"
    src="$HERE/$TRANSCRIPT"
    if [ ! -f "$src" ]; then
        echo "missing transcript: $src" >&2
        exit 1
    fi
    echo "Claude Code -> $dst"
    if [ "$DRY" = 1 ]; then
        echo "DRY-RUN: nothing written"
        return 0
    fi
    if [ -e "$dst" ] && [ "$FORCE" = 0 ]; then
        echo "refusing to overwrite: $dst (use --force)" >&2
        exit 1
    fi
    rewrite_file "$src" "$dst" || exit 1
    if [ -d "$HERE/subagents" ]; then
        for f in "$HERE"/subagents/*.jsonl; do
            [ -e "$f" ] || continue
            out="$project/$NEW_ID/subagents/$(basename "$f")"
            if [ -e "$out" ] && [ "$FORCE" = 0 ]; then
                echo "refusing to overwrite: $out (use --force)" >&2
                exit 1
            fi
            rewrite_file "$f" "$out" || exit 1
        done
    fi
    echo ""
    echo "Claude session '$NEW_ID' installed for: $CWD"
    echo "Run:  cd \"$CWD\"; claude   then  /resume"
}

install_codex() {
    home=$(codex_home)
    py=$(find_python || true)
    if [ -n "$py" ] && [ -f "$HERE/install.py" ]; then
        echo "Using $py $HERE/install.py"
        extra=""
        [ "$FORCE" = 1 ] && extra="--force"
        [ "$DRY" = 1 ] && extra="$extra --dry-run"
        "$py" "$HERE/install.py" --cwd "$CWD" --codex-home "$home" $extra
        return $?
    fi

    now=$(date +%Y-%m-%dT%H-%M-%S)
    y=$(date +%Y)
    mo=$(date +%m)
    d=$(date +%d)
    dir="$home/sessions/$y/$mo/$d"
    dst="$dir/rollout-$now-$NEW_ID.jsonl"
    src="$HERE/$TRANSCRIPT"
    if [ ! -f "$src" ]; then
        echo "missing transcript: $src" >&2
        exit 1
    fi
    echo "Codex -> $dst"
    if [ "$DRY" = 1 ]; then
        echo "DRY-RUN: nothing written"
        db=$(find_state_db "$home")
        [ -n "$db" ] || db="$home/state_5.sqlite"
        echo "DRY-RUN SQL for $db :"
        codex_sql "$dst"
        return 0
    fi
    if [ -e "$dst" ] && [ "$FORCE" = 0 ]; then
        echo "refusing to overwrite: $dst (use --force)" >&2
        exit 1
    fi
    rewrite_file "$src" "$dst" || exit 1

    db=$(find_state_db "$home")
    [ -n "$db" ] || db="$home/state_5.sqlite"
    sql=$(codex_sql "$dst")
    if command -v sqlite3 >/dev/null 2>&1 && [ -f "$db" ]; then
        if printf '%s\n' "$sql" | sqlite3 "$db"; then
            echo "registered thread $NEW_ID in $db"
        else
            echo "warning: sqlite3 failed to register the thread; run this SQL manually:" >&2
            printf '%s\n' "$sql"
        fi
    else
        echo "warning: sqlite3 not found (or no state DB); the rollout is copied but not registered."
        echo "Run this SQL in $db :"
        printf '%s\n' "$sql"
    fi
    echo ""
    echo "Codex session '$NEW_ID' installed for: $CWD"
    echo "Run:  cd \"$CWD\"; codex   then  /resume"
}

case "$SOURCE" in
    claude) install_claude ;;
    codex) install_codex ;;
    *) echo "unknown source in metadata.json: $SOURCE" >&2; exit 1 ;;
esac
'''


# ---------------------------------------------------------------------------
# Cross-tool conversion scripts. The bundle already carries every target's
# transcript pre-rendered (transplant/), with the working directory left as
# CWD_PLACEHOLDER; these scripts substitute the requested folder and install.
# ---------------------------------------------------------------------------

CONVERT_PY = r'''#!/usr/bin/env python3
"""Transplant this bundle into another AI tool's session store.

    python convert.py claude   [--cwd DIR]
    python convert.py codex    [--cwd DIR]
    python convert.py opencode [--cwd DIR]
    python convert.py gemini   [--cwd DIR]
    python convert.py qwen     [--cwd DIR]
    python convert.py goose    [--cwd DIR]
    python convert.py cline    [--cwd DIR]
    python convert.py roo      [--cwd DIR]
    python convert.py json     [--cwd DIR]

Reading only the bundle: the transcript for every target was rendered at
export time. This script rewrites the working-directory placeholder and
installs the result. Python 3, stdlib only.
"""
from __future__ import annotations

import argparse
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
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PH = "__ADM_TARGET_CWD__"


def meta() -> dict:
    return json.loads((HERE / "metadata.json").read_text(encoding="utf-8"))


def _esc(p: str) -> str:
    return json.dumps(p, ensure_ascii=False)[1:-1]


def render(path: Path, cwd: str, *, json_text: bool = True) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.replace(PH, _esc(cwd) if json_text else cwd)


def write(path: Path, text: str, dry: bool, force: bool) -> None:
    if path.exists() and not force:
        sys.exit(f"refusing to overwrite: {path}\n(use --force)")
    print(("DRY-RUN would write " if dry else "write ") + str(path))
    if dry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def munge(p: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def claude_home(a) -> Path:
    if a.claude_home:
        return Path(a.claude_home).expanduser()
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def codex_home(a) -> Path:
    if a.codex_home:
        return Path(a.codex_home).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _safe(s: str, limit: int = 60) -> str:
    s = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", s or "").strip(" ._")
    return s[:limit].rstrip(" ._") or "session"


def find_state_db(root: Path, override: str | None) -> Path | None:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("CODEX_STATE_DB")
    if env:
        return Path(env).expanduser()
    p = root / "state_5.sqlite"
    if p.exists():
        return p
    cands = sorted(root.glob("state_*.sqlite"),
                   key=lambda x: int(re.sub(r"\D", "", x.stem) or 0), reverse=True)
    return cands[0] if cands else None


def register_thread(db: Path, md: dict, sid: str, cwd: str, rollout: Path,
                    dry: bool, force: bool) -> bool:
    try:
        con = sqlite3.connect(str(db), timeout=8)
    except sqlite3.Error as e:
        print(f"warning: cannot open {db}: {e}")
        return False
    try:
        info = list(con.execute("PRAGMA table_info(threads)"))
        if not info:
            print(f"warning: {db} has no threads table")
            return False
        cols = [r[1] for r in info]
        required = {r[1]: (r[2] or "", r[3], r[4]) for r in info}
        now = int(time.time())
        now_ms = int(time.time() * 1000)
        title = md.get("title") or ""
        preview = md.get("preview") or title
        row = dict(md.get("codex_thread") or {})
        vals = dict(row)
        vals.update({
            "id": sid, "rollout_path": str(rollout), "cwd": cwd,
            "title": title, "archived": 0, "archived_at": None,
            "created_at": now, "updated_at": now,
            "created_at_ms": now_ms, "updated_at_ms": now_ms,
            "recency_at": now, "recency_at_ms": now_ms, "is_pinned": 0,
            "first_user_message": preview, "preview": preview,
            "thread_section_id": None, "project_id": None,
            "section_position": None, "section_entered_at_ms": None,
            "attached_thread_parent_id": None, "source": "cli",
            "model": md.get("model") or row.get("model") or "custom",
            "model_provider": row.get("model_provider") or "openai",
            "sandbox_policy": row.get("sandbox_policy") or "danger-full-access",
            "approval_mode": row.get("approval_mode") or "never",
        })
        use = [c for c in cols if c in vals and vals[c] is not None]
        for name, (typ, notnull, dflt) in required.items():
            if name in use or not notnull or dflt is not None:
                continue
            typ = typ.upper()
            vals[name] = "" if ("CHAR" in typ or "TEXT" in typ) else 0
            use.append(name)
        use = [c for c in cols if c in use]
        if con.execute("SELECT 1 FROM threads WHERE id=?", (sid,)).fetchone() \
                and not force:
            print(f"warning: thread {sid} already exists (use --force)")
            return False
        if dry:
            print(f"DRY-RUN would insert thread {sid} into {db}")
            return True
        con.execute(
            f"INSERT OR REPLACE INTO threads ({','.join(use)}) "
            f"VALUES ({','.join('?' * len(use))})",
            [vals[c] for c in use])
        con.commit()
        print(f"registered thread {sid} in {db}")
        return True
    except sqlite3.Error as e:
        print(f"warning: could not register thread: {e}")
        return False
    finally:
        con.close()


def install_claude(md: dict, cwd: str, a) -> int:
    t = md["transplant"]["claude"]
    sid = a.id or t["id"]
    dest = claude_home(a) / "projects" / munge(cwd) / f"{sid}.jsonl"
    text = render(HERE / t["file"], cwd).replace(t["id"], sid)
    write(dest, text, a.dry_run, a.force)
    print(f"\nClaude session '{sid}' installed for:\n  {cwd}")
    print("Afterwards run:  claude   ->  /resume")
    return 0


def install_codex(md: dict, cwd: str, a) -> int:
    t = md["transplant"]["codex"]
    sid = a.id or t["id"]
    root = codex_home(a)
    now = datetime.now()
    name = t.get("name") or f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
    rollout = (root / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
               / name)
    text = render(HERE / t["file"], cwd).replace(t["id"], sid)
    write(rollout, text, a.dry_run, a.force)
    db = find_state_db(root, a.state_db)
    if db and db.exists():
        register_thread(db, md, sid, cwd, rollout, a.dry_run, a.force)
    else:
        print(f"warning: no Codex state DB under {root}; copied the rollout only")
    print(f"\nCodex session '{sid}' installed for:\n  {cwd}")
    print("Afterwards run:  codex   ->  /resume")
    return 0


def _opencode_service() -> tuple[str, str]:
    cli = shutil.which("opencode")
    if not cli:
        sys.exit("opencode CLI not found — install OpenCode or use another target")
    try:
        out = subprocess.run([cli, "api", "get", "/api/info"],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        sys.exit(f"cannot reach OpenCode: {e}")
    if out.returncode != 0:
        sys.exit("OpenCode service is not reachable — start `opencode` once, "
                 "then retry.\n" + (out.stderr or out.stdout or "")[:400])
    try:
        info = json.loads(out.stdout)
    except ValueError:
        sys.exit("unexpected `opencode api` output: " + (out.stdout or "")[:300])
    urls = info.get("urls") or []
    if not urls:
        sys.exit("OpenCode service did not report a URL")
    pw = os.environ.get("OPENCODE_PASSWORD") \
        or os.environ.get("OPENCODE_SERVER_PASSWORD") or ""
    if not pw:
        cfg = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
               / "opencode" / "service.json")
        try:
            pw = json.loads(cfg.read_text(encoding="utf-8")).get("password", "")
        except (OSError, ValueError):
            pw = ""
    if not pw:
        sys.exit("OpenCode service password not found "
                 "(~/.config/opencode/service.json)")
    return urls[0].rstrip("/"), pw


def install_opencode(md: dict, cwd: str, a) -> int:
    t = md["transplant"]["opencode"]
    payload = json.loads(render(HERE / t["file"], cwd))
    if a.dry_run:
        print(f"DRY-RUN would POST session '{t['id']}' to OpenCode")
        return 0
    base, pw = _opencode_service()
    token = base64.b64encode(("opencode:" + pw).encode()).decode()
    req = urllib.request.Request(
        base + "/api/experimental/session/import",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Basic " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"OpenCode import failed ({e.code}): "
                 + e.read().decode("utf-8", "replace")[:600])
    except urllib.error.URLError as e:
        sys.exit(f"cannot connect to OpenCode: {e}")
    info = (data or {}).get("data") or {}
    print(f"\nOpenCode session '{info.get('id') or t['id']}' imported for:\n  {cwd}")
    print("Afterwards run:  opencode   (pick it from the session list)")
    return 0


def _gem_home(a, tool: str) -> Path:
    override = a.gemini_home if tool == "gemini" else a.qwen_home
    if override:
        return Path(override).expanduser()
    env = os.environ.get("GEMINI_CLI_HOME" if tool == "gemini"
                         else "QWEN_HOME")
    return Path(env).expanduser() if env else Path.home() / f".{tool}"


def install_gemini(md: dict, cwd: str, a, tool: str = "gemini") -> int:
    t = md["transplant"][tool]
    sid = a.id or t["id"]
    doc = json.loads(render(HERE / t["file"], cwd))
    doc["sessionId"] = sid
    doc["projectHash"] = hashlib.sha256(cwd.encode("utf-8")).hexdigest()
    doc["lastUpdated"] = datetime.now().isoformat() + "Z"
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    dest = (_gem_home(a, tool) / "tmp" / doc["projectHash"] / "chats"
            / f"session-{stamp}-{sid}.json")
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    write(dest, text, a.dry_run, a.force)
    print(f"\n{tool} session '{sid}' installed for:\n  {cwd}")
    print(f"Afterwards run:  {tool} --resume {sid}")
    return 0


def _goose_home(a) -> Path:
    if a.goose_home:
        return Path(a.goose_home).expanduser()
    if os.environ.get("GOOSE_HOME"):
        return Path(os.environ["GOOSE_HOME"]).expanduser()
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


def install_goose(md: dict, cwd: str, a) -> int:
    t = md["transplant"]["goose"]
    sid = a.id or t["id"]
    doc = json.loads(render(HERE / t["file"], cwd))
    doc["id"] = sid
    doc["working_dir"] = cwd
    dest = _goose_home(a) / f"{sid}.json"
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    write(dest, text, a.dry_run, a.force)
    print(f"\nGoose session '{sid}' installed for:\n  {cwd}")
    print(f"Afterwards run:  goose session --resume --name {sid}")
    return 0


def _vscode_storage(a) -> Path:
    if a.vscode_storage:
        return Path(a.vscode_storage).expanduser()
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
    return insiders if (not stable.exists() and insiders.exists()) else stable


def install_cline(md: dict, cwd: str, a, tool: str = "cline") -> int:
    t = md["transplant"][tool]
    sid = a.id or t["id"]
    ext = ("saoudrizwan.claude-dev" if tool == "cline"
           else "rooveterinaryinc.roo-cline")
    dest = _vscode_storage(a) / ext / "tasks" / sid
    src = HERE / t["dir"]
    names = ("api_conversation_history.json", "ui_messages.json",
             "task_metadata.json")
    if a.dry_run:
        print(f"DRY-RUN would write the {tool} task to {dest}")
        return 0
    if dest.exists() and not a.force:
        sys.exit(f"refusing to overwrite: {dest} (use --force)")
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        text = render(src / name, cwd)
        (dest / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"write {dest / name}")
    print(f"\n{tool} task '{sid}' installed for:\n  {cwd}")
    print("Open VS Code, then the extension's history list to continue it.")
    return 0


def install_json(md: dict, cwd: str, a) -> int:
    t = md["transplant"]["json"]
    src = HERE / t["dir"]
    tag = md["transplant"]["codex"]["id"][:8]
    folder = Path(cwd) / f"{_safe(md.get('title') or 'session')}-{tag}"
    if a.dry_run:
        print(f"DRY-RUN would copy the neutral transcript to {folder}")
        return 0
    if folder.exists() and not a.force:
        sys.exit(f"refusing to overwrite: {folder} (use --force)")
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("transcript.json", "transcript.md", "README.md"):
        text = render(src / name, cwd, json_text=name.endswith(".json"))
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"\nPortable transcript written to:\n  {folder}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Transplant this bundle")
    ap.add_argument("target", choices=["claude", "codex", "opencode", "gemini",
                                       "qwen", "goose", "cline", "roo", "json"])
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--id", default=None)
    ap.add_argument("--claude-home", default=None)
    ap.add_argument("--codex-home", default=None)
    ap.add_argument("--state-db", default=None)
    ap.add_argument("--gemini-home", default=None)
    ap.add_argument("--qwen-home", default=None)
    ap.add_argument("--goose-home", default=None)
    ap.add_argument("--vscode-storage", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    md = meta()
    if not md.get("transplant"):
        sys.exit("this bundle carries no cross-tool transcripts")
    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    Path(cwd).mkdir(parents=True, exist_ok=True)
    return {
        "claude": install_claude,
        "codex": install_codex,
        "opencode": install_opencode,
        "gemini": lambda m, c, x: install_gemini(m, c, x, "gemini"),
        "qwen": lambda m, c, x: install_gemini(m, c, x, "qwen"),
        "goose": install_goose,
        "cline": lambda m, c, x: install_cline(m, c, x, "cline"),
        "roo": lambda m, c, x: install_cline(m, c, x, "roo"),
        "json": install_json,
    }[a.target](md, cwd, a)


if __name__ == "__main__":
    raise SystemExit(main())
'''


CONVERT_BAT = r'''@echo off
rem Transplant this bundle into another AI tool.
rem Usage: convert.bat claude|codex|opencode|gemini|qwen|goose|cline|roo|json ["target working folder"]
setlocal
set "HERE=%~dp0"
if "%~1"=="" goto :usage
set "TARGET=%~1"
if "%~2"=="" goto :ask
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%convert.ps1" -Target "%TARGET%" -Cwd "%~2"
exit /b %ERRORLEVEL%

:ask
echo Transplant to: %TARGET%
set "WORK=%CD%"
set /p "WORK=Target working folder [%CD%]: "
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%convert.ps1" -Target "%TARGET%" -Cwd "%WORK%"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Transplant failed with exit code %RC%.
pause
exit /b %RC%

:usage
echo usage: convert.bat claude^|codex^|opencode^|gemini^|qwen^|goose^|cline^|roo^|json ["target working folder"]
exit /b 2
'''


# Windows PowerShell cross-tool converter. ASCII only (see INSTALL_PS1).
CONVERT_PS1 = r'''# Transplant this bundle into another AI tool.
# Usage:
#   powershell -ExecutionPolicy Bypass -File convert.ps1 -Target claude|codex|opencode|gemini|qwen|goose|cline|roo|json
#       [-Cwd DIR] [-Id ID] [-ClaudeHome DIR] [-CodexHome DIR] [-StateDb FILE]
#       [-DryRun] [-Force]
param(
    [Parameter(Mandatory = $true)][ValidateSet("claude","codex","opencode","gemini","qwen","goose","cline","roo","json")]
    [string]$Target,
    [string]$Cwd = "",
    [string]$Id = "",
    [string]$ClaudeHome = "",
    [string]$CodexHome = "",
    [string]$StateDb = "",
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
if (-not $Here) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
$MetaPath = Join-Path $Here "metadata.json"
if (-not (Test-Path -LiteralPath $MetaPath)) {
    throw "metadata.json not found next to convert.ps1 ($Here)"
}
$meta = Get-Content -LiteralPath $MetaPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $meta.transplant) { throw "this bundle carries no cross-tool transcripts" }
$ph = "__ADM_TARGET_CWD__"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$title = (([string]$meta.title) -replace "\s+", " ").Trim()
if ($title.Length -gt 200) { $title = $title.Substring(0, 200) }
if (-not $title) { $title = "transplanted session" }

if ($Cwd) { $work = [System.IO.Path]::GetFullPath($Cwd) }
else { $work = [System.IO.Path]::GetFullPath((Get-Location).Path) }
if (-not (Test-Path -LiteralPath $work)) { New-Item -ItemType Directory -Path $work -Force | Out-Null }
$trimmed = $work.TrimEnd([char[]]@("\", "/"))
if ($trimmed -match "^[A-Za-z]:$") { $work = $trimmed + "\" } else { $work = $trimmed }

# The placeholder sits *inside* a JSON string in the pre-rendered
# transcripts, so only escape it - never add quotes here.
function JsonEscape([string]$p) {
    if ([string]::IsNullOrEmpty($p)) { return "" }
    return $p.Replace("\", "\\").Replace('"', '\"')
}

function Render([string]$path, [string]$cwd) {
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    return $text.Replace($ph, (JsonEscape $cwd))
}

function Render-Text([string]$path, [string]$cwd) {
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    return $text.Replace($ph, $cwd)
}

function Write-Target([string]$dest, [string]$text) {
    if ((Test-Path -LiteralPath $dest) -and (-not $Force)) {
        throw "refusing to overwrite: $dest (use -Force)"
    }
    if ($DryRun) { Write-Host ("DRY-RUN would write " + $dest); return }
    $dir = Split-Path -Parent $dest
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($dest, $text, $Utf8)
    Write-Host ("write " + $dest)
}

function Get-ClaudeHome {
    if ($ClaudeHome) { return $ClaudeHome }
    if ($env:CLAUDE_CONFIG_DIR) { return $env:CLAUDE_CONFIG_DIR }
    return (Join-Path $HOME ".claude")
}

function Get-CodexHome {
    if ($CodexHome) { return $CodexHome }
    if ($env:CODEX_HOME) { return $env:CODEX_HOME }
    return (Join-Path $HOME ".codex")
}

function Find-Python {
    foreach ($name in @("py.exe", "python.exe", "python3.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            & $cmd.Source -c "import sys" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $cmd.Source }
        } catch {
        }
    }
    return $null
}

function Find-StateDb([string]$root) {
    if ($StateDb) { return $StateDb }
    if ($env:CODEX_STATE_DB) { return $env:CODEX_STATE_DB }
    $p5 = Join-Path $root "state_5.sqlite"
    if (Test-Path -LiteralPath $p5) { return $p5 }
    $c = Get-ChildItem -LiteralPath $root -Filter "state_*.sqlite" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($c) { return $c.FullName }
    return $p5
}

function SqlQuote([string]$s) {
    if ($null -eq $s) { $s = "" }
    return "'" + $s.Replace("'", "''") + "'"
}

function Convert-Claude {
    $t = $meta.transplant.claude
    $sid = if ($Id) { $Id } else { [string]$t.id }
    $munged = $work -replace "[^A-Za-z0-9]", "-"
    $dest = Join-Path (Join-Path (Join-Path (Get-ClaudeHome) "projects") $munged) ($sid + ".jsonl")
    $text = Render (Join-Path $Here ([string]$t.file)) $work
    if ($sid -ne [string]$t.id) { $text = $text.Replace([string]$t.id, $sid) }
    Write-Target $dest $text
    Write-Host ""
    Write-Host ("Claude session '" + $sid + "' installed for: " + $work)
    Write-Host "Run:  cd `"$work`"; claude   then  /resume"
}

function Convert-Codex {
    $t = $meta.transplant.codex
    $sid = if ($Id) { $Id } else { [string]$t.id }
    $root = Get-CodexHome
    $now = Get-Date
    $name = [string]$t.name
    if (-not $name) { $name = "rollout-" + $now.ToString("yyyy-MM-ddTHH-mm-ss") + "-" + $sid + ".jsonl" }
    $dest = Join-Path (Join-Path (Join-Path (Join-Path $root "sessions") $now.ToString("yyyy")) $now.ToString("MM")) $now.ToString("dd")
    $dest = Join-Path $dest $name
    $text = Render (Join-Path $Here ([string]$t.file)) $work
    if ($sid -ne [string]$t.id) { $text = $text.Replace([string]$t.id, $sid) }
    Write-Target $dest $text

    $db = Find-StateDb $root
    $py = Find-Python
    if ($DryRun) {
        Write-Host ("DRY-RUN would register thread " + $sid + " in " + $db)
    } elseif ($py) {
        $a = @((Join-Path $Here "convert.py"), "codex", "--cwd", $work,
               "--codex-home", $root, "--id", $sid, "--force")
        if ($StateDb) { $a += @("--state-db", $StateDb) }
        & $py @a
    } else {
        $nowSec = [long][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $preview = (([string]$meta.preview) -replace "\s+", " ").Trim()
        if ($preview.Length -gt 200) { $preview = $preview.Substring(0, 200) }
        $sql = "INSERT OR REPLACE INTO threads " +
            "(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, " +
            "sandbox_policy, approval_mode, tokens_used, has_user_event, archived, first_user_message, preview) VALUES (" +
            (SqlQuote $sid) + ", " + (SqlQuote $dest) + ", " + $nowSec + ", " + $nowSec +
            ", 'cli', 'openai', " + (SqlQuote $work) + ", " + (SqlQuote $title) +
            ", 'danger-full-access', 'never', 0, 0, 0, " + (SqlQuote $preview) + ", " + (SqlQuote $preview) + ");"
        $sqlite = Get-Command "sqlite3.exe" -ErrorAction SilentlyContinue
        if (-not $sqlite) { $sqlite = Get-Command "sqlite3" -ErrorAction SilentlyContinue }
        if ($sqlite -and (Test-Path -LiteralPath $db)) {
            $sql | & $sqlite.Source $db
            if ($LASTEXITCODE -eq 0) { Write-Host ("registered thread " + $sid + " in " + $db) }
            else { Write-Host "warning: sqlite3 failed; run this SQL manually:"; Write-Host $sql }
        } else {
            Write-Host "warning: sqlite3 not found; the rollout is copied but not registered."
            Write-Host ("Run this SQL in " + $db + " :")
            Write-Host $sql
        }
    }
    Write-Host ""
    Write-Host ("Codex session '" + $sid + "' installed for: " + $work)
    Write-Host "Run:  cd `"$work`"; codex   then  /resume"
}

function Convert-OpenCode {
    $t = $meta.transplant.opencode
    $payload = Render (Join-Path $Here ([string]$t.file)) $work
    if ($DryRun) { Write-Host ("DRY-RUN would POST session " + $t.id + " to OpenCode"); return }
    $cli = Get-Command "opencode" -ErrorAction SilentlyContinue
    if (-not $cli) { throw "opencode CLI not found - pick another target" }
    $infoJson = & $cli.Source api get /api/info
    if ($LASTEXITCODE -ne 0) { throw "OpenCode service is not reachable - start 'opencode' once, then retry" }
    $info = $infoJson | ConvertFrom-Json
    $base = ([string]($info.urls[0])).TrimEnd("/")
    $pw = $env:OPENCODE_PASSWORD
    if (-not $pw) { $pw = $env:OPENCODE_SERVER_PASSWORD }
    if (-not $pw) {
        $cfg = Join-Path (Join-Path $HOME ".config") "opencode\service.json"
        if (Test-Path -LiteralPath $cfg) {
            $pw = (Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).password
        }
    }
    if (-not $pw) { throw "OpenCode service password not found (~/.config/opencode/service.json)" }
    $token = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("opencode:" + $pw))
    try {
        $r = Invoke-RestMethod -Uri ($base + "/api/experimental/session/import") -Method Post `
            -Headers @{ Authorization = "Basic " + $token } `
            -ContentType "application/json; charset=utf-8" `
            -Body ([Text.Encoding]::UTF8.GetBytes($payload))
    } catch {
        throw ("OpenCode import failed: " + $_.Exception.Message + " " + $_.ErrorDetails.Message)
    }
    $sid = [string]$r.data.id
    Write-Host ""
    Write-Host ("OpenCode session '" + $sid + "' imported for: " + $work)
    Write-Host "Run:  cd `"$work`"; opencode   (pick it from the session list)"
}

function Convert-Json {
    $t = $meta.transplant.json
    $src = Join-Path $Here ([string]$t.dir)
    $tag = ([string]$meta.transplant.codex.id).Substring(0, 8)
    $safe = ($title -replace '[<>:"/\\|?*\r\n\t]', "_").Trim(" ._")
    if (-not $safe) { $safe = "session" }
    $dest = Join-Path $work ($safe + "-" + $tag)
    if ($DryRun) { Write-Host ("DRY-RUN would write the neutral transcript to " + $dest); return }
    if ((Test-Path -LiteralPath $dest) -and (-not $Force)) {
        throw "refusing to overwrite: $dest (use -Force)"
    }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    foreach ($name in @("transcript.json", "transcript.md", "README.md")) {
        if ($name.EndsWith(".json")) {
            $text = Render (Join-Path $src $name) $work
        } else {
            $text = Render-Text (Join-Path $src $name) $work
        }
        [System.IO.File]::WriteAllText((Join-Path $dest $name), $text, $Utf8)
    }
    Write-Host ("Portable transcript written to: " + $dest)
}

function Convert-ViaPython([string]$target) {
    $py = Find-Python
    if (-not $py) {
        throw ($target + " needs Python 3 on this machine; install Python " +
               "or run convert.py manually")
    }
    $a = @((Join-Path $Here "convert.py"), $target, "--cwd", $work)
    if ($Id) { $a += @("--id", $Id) }
    if ($DryRun) { $a += "--dry-run" }
    if ($Force) { $a += "--force" }
    & $py @a
    if ($LASTEXITCODE -ne 0) {
        throw ("convert.py " + $target + " failed with exit code " + $LASTEXITCODE)
    }
}

switch ($Target) {
    "claude"   { Convert-Claude }
    "codex"    { Convert-Codex }
    "opencode" { Convert-OpenCode }
    "gemini"   { Convert-ViaPython "gemini" }
    "qwen"     { Convert-ViaPython "qwen" }
    "goose"    { Convert-ViaPython "goose" }
    "cline"    { Convert-ViaPython "cline" }
    "roo"      { Convert-ViaPython "roo" }
    "json"     { Convert-Json }
}
'''


# POSIX cross-tool converter: Claude and the neutral transcript need no
# Python; Codex registration uses convert.py, then sqlite3, then prints SQL;
# OpenCode uses curl.
CONVERT_SH = r'''#!/bin/sh
# Transplant this bundle into another AI tool.
# Usage: sh convert.sh claude|codex|opencode|gemini|qwen|goose|cline|roo|json [WORKDIR] [--dry-run] [--force]
#   WORKDIR defaults to the current directory.
set -u

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PH='__ADM_TARGET_CWD__'
TARGET=""
CWD="$PWD"
DRY=0
FORCE=0

usage() {
    echo "usage: sh convert.sh claude|codex|opencode|gemini|qwen|goose|cline|roo|json [WORKDIR] [--dry-run] [--force]"
}

for arg in "$@"; do
    case "$arg" in
        claude|codex|opencode|gemini|qwen|goose|cline|roo|json) TARGET="$arg" ;;
        --dry-run) DRY=1 ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) CWD="$arg" ;;
    esac
done
[ -n "$TARGET" ] || { usage; exit 2; }
[ -d "$CWD" ] || { echo "working folder not found: $CWD" >&2; exit 1; }
CWD=$(CDPATH= cd -- "$CWD" && pwd -P) || exit 1

json_get() {
    key=$1
    sed -n 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$HERE/metadata.json" | head -n 1
}

# new_id of a pre-rendered transcript (metadata.json ordering is not assumed)
file_id() {
    f=$1
    if grep -q session_meta "$f" 2>/dev/null; then
        sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$f" | head -n 1
    else
        sed -n 's/.*"sessionId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$f" | head -n 1
    fi
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

render() {
    src=$1
    new=$(json_escape "$CWD")
    sed "s|$PH|$new|g" "$src"
}

# `python` may be the Windows Store stub, which fails on any -c invocation
find_python() {
    for cand in python3 python; do
        p=$(command -v "$cand" 2>/dev/null) || continue
        if "$p" -c "import sys" >/dev/null 2>&1; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

render_text() {
    sed "s|$PH|$CWD|g" "$1"
}

write_file() {
    dst=$1
    src=$2
    if [ "$DRY" = 1 ]; then
        echo "DRY-RUN would write $dst"
        return 0
    fi
    if [ -e "$dst" ] && [ "$FORCE" = 0 ]; then
        echo "refusing to overwrite: $dst (use --force)" >&2
        return 1
    fi
    mkdir -p "$(dirname "$dst")" || return 1
    render "$src" > "$dst".tmp || return 1
    mv "$dst".tmp "$dst"
    echo "write $dst"
}

claude_home() {
    if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
        printf '%s' "$CLAUDE_CONFIG_DIR"
    else
        printf '%s/.claude' "${HOME:-$CWD}"
    fi
}

codex_home() {
    if [ -n "${CODEX_HOME:-}" ]; then
        printf '%s' "$CODEX_HOME"
    else
        printf '%s/.codex' "${HOME:-$CWD}"
    fi
}

do_claude() {
    sid=$(file_id "$HERE/transplant/claude.jsonl")
    [ -n "$sid" ] || sid=$(json_get new_id)
    munged=$(printf '%s' "$CWD" | sed 's/[^A-Za-z0-9]/-/g')
    dst="$(claude_home)/projects/$munged/$sid.jsonl"
    write_file "$dst" "$HERE/transplant/claude.jsonl" || exit 1
    echo ""
    echo "Claude session '$sid' installed for: $CWD"
    echo "Run:  cd \"$CWD\"; claude   then  /resume"
}

sql_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"
}

do_codex() {
    sid=$(file_id "$HERE/transplant/codex.jsonl")
    [ -n "$sid" ] || sid=$(json_get new_id)
    home=$(codex_home)
    name=$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$HERE/metadata.json" | head -n 1)
    [ -n "$name" ] || name="rollout-$(date +%Y-%m-%dT%H-%M-%S)-$sid.jsonl"
    dir="$home/sessions/$(date +%Y)/$(date +%m)/$(date +%d)"
    dst="$dir/$name"
    write_file "$dst" "$HERE/transplant/codex.jsonl" || exit 1

    db="${CODEX_STATE_DB:-}"
    if [ -z "$db" ]; then
        if [ -f "$home/state_5.sqlite" ]; then
            db="$home/state_5.sqlite"
        else
            db=$(for f in "$home"/state_*.sqlite; do [ -e "$f" ] && echo "$f"; done \
                 | sort | tail -n 1)
        fi
    fi
    [ -n "$db" ] || db="$home/state_5.sqlite"

    py=$(find_python || true)
    if [ "$DRY" = 1 ]; then
        echo "DRY-RUN would register thread $sid in $db"
    elif [ -n "$py" ] && [ -f "$HERE/convert.py" ]; then
        "$py" "$HERE/convert.py" codex --cwd "$CWD" --codex-home "$home" \
             --id "$sid" --force
    else
        title=$(json_get title)
        now_s=$(date +%s)
        sql="INSERT OR REPLACE INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event, archived) VALUES ($(sql_quote "$sid"), $(sql_quote "$dst"), $now_s, $now_s, 'cli', 'openai', $(sql_quote "$CWD"), $(sql_quote "$title"), 'danger-full-access', 'never', 0, 0, 0);"
        if command -v sqlite3 >/dev/null 2>&1 && [ -f "$db" ]; then
            if printf '%s\n' "$sql" | sqlite3 "$db"; then
                echo "registered thread $sid in $db"
            else
                echo "warning: sqlite3 failed; run this SQL manually:" >&2
                printf '%s\n' "$sql"
            fi
        else
            echo "warning: sqlite3 not found; the rollout is copied but not registered."
            echo "Run this SQL in $db :"
            printf '%s\n' "$sql"
        fi
    fi
    echo ""
    echo "Codex session '$sid' installed for: $CWD"
    echo "Run:  cd \"$CWD\"; codex   then  /resume"
}

do_opencode() {
    if [ "$DRY" = 1 ]; then
        echo "DRY-RUN would POST the bundled session to OpenCode"
        return 0
    fi
    py=$(find_python || true)
    if [ -n "$py" ] && [ -f "$HERE/convert.py" ]; then
        "$py" "$HERE/convert.py" opencode --cwd "$CWD" --force
        return $?
    fi
    base=""
    if command -v opencode >/dev/null 2>&1; then
        # Git-Bash/MSYS rewrites the "/api/info" argument into a Windows path,
        # which breaks the CLI's routing; opt out for this invocation.
        MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1 \
            opencode api get /api/info 2>/dev/null > "$HERE/.adm-oc-info.tmp" || true
        base=$(sed -n 's/.*"urls"[[:space:]]*:[[:space:]]*\[[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$HERE/.adm-oc-info.tmp" 2>/dev/null | head -n 1)
        rm -f "$HERE/.adm-oc-info.tmp"
    fi
    [ -n "$base" ] || { echo "OpenCode service is not reachable - start 'opencode' once, then retry" >&2; exit 1; }
    pw="${OPENCODE_PASSWORD:-${OPENCODE_SERVER_PASSWORD:-}}"
    if [ -z "$pw" ]; then
        pw=$(sed -n 's/.*"password"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/service.json" 2>/dev/null | head -n 1)
    fi
    [ -n "$pw" ] || { echo "OpenCode service password not found" >&2; exit 1; }
    command -v curl >/dev/null 2>&1 || { echo "curl not found - install curl or use convert.py" >&2; exit 1; }
    body=$(render "$HERE/transplant/opencode.json")
    printf '%s' "$body" | curl -sS -u "opencode:$pw" -H "Content-Type: application/json" \
        --data-binary @- "$base/api/experimental/session/import" >/dev/null || exit 1
    echo ""
    echo "OpenCode session imported for: $CWD"
    echo "Run:  cd \"$CWD\"; opencode   (pick it from the session list)"
}

do_json() {
    tag=$(file_id "$HERE/transplant/codex.jsonl" | cut -c1-8)
    [ -n "$tag" ] || tag=session
    title=$(json_get title)
    safe=$(printf '%s' "$title" | sed 's/[<>:"\/\\|?*]/_/g' | cut -c1-60)
    [ -n "$safe" ] || safe=session
    dst="$CWD/$safe-$tag"
    if [ "$DRY" = 1 ]; then
        echo "DRY-RUN would write the neutral transcript to $dst"
        return 0
    fi
    if [ -e "$dst" ] && [ "$FORCE" = 0 ]; then
        echo "refusing to overwrite: $dst (use --force)" >&2
        return 1
    fi
    mkdir -p "$dst" || return 1
    for f in transcript.json transcript.md README.md; do
        case "$f" in
            *.json) render "$HERE/transplant/neutral/$f" > "$dst/$f" ;;
            *) render_text "$HERE/transplant/neutral/$f" > "$dst/$f" ;;
        esac
    done
    echo "Portable transcript written to: $dst"
}

do_via_python() {
    t=$1
    py=$(find_python || true)
    if [ -z "$py" ]; then
        echo "$t needs Python 3 on this machine; install Python or run convert.py manually" >&2
        return 1
    fi
    pyargs=""
    [ "$DRY" = 1 ] && pyargs="$pyargs --dry-run"
    [ "$FORCE" = 1 ] && pyargs="$pyargs --force"
    # shellcheck disable=SC2086
    "$py" "$HERE/convert.py" "$t" --cwd "$CWD" $pyargs
}

case "$TARGET" in
    claude) do_claude ;;
    codex) do_codex ;;
    opencode) do_opencode ;;
    gemini|qwen|goose|cline|roo) do_via_python "$TARGET" ;;
    json) do_json ;;
esac
'''


# ---------------------------------------------------------------------------
# One-click restore / continue scripts.  convert.* only installs the
# transcript; these wrappers also reopen the target CLI on the new session.
# They are deliberately thin: all conversion lives in convert.*.
# ---------------------------------------------------------------------------

RESTORE_PS1 = r'''# Restore this bundle into another AI tool and continue the session.
# Usage:
#   powershell -ExecutionPolicy Bypass -File restore.ps1 -Target claude|codex|opencode|gemini|qwen|goose|cline|roo|json
#       [-Cwd DIR] [-Id ID] [-ClaudeHome DIR] [-CodexHome DIR] [-StateDb FILE]
#       [-NoLaunch] [-Force] [-DryRun]
# The transplant itself is done by convert.ps1; this script only adds the
# "resume the new session in a new terminal" step.
param(
    [Parameter(Mandatory = $true)][ValidateSet("claude","codex","opencode","gemini","qwen","goose","cline","roo","json")]
    [string]$Target,
    [string]$Cwd = "",
    [string]$Id = "",
    [string]$ClaudeHome = "",
    [string]$CodexHome = "",
    [string]$StateDb = "",
    [switch]$NoLaunch,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
if (-not $Here) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Conv = Join-Path $Here "convert.ps1"
if (-not (Test-Path -LiteralPath $Conv)) {
    throw "convert.ps1 not found next to restore.ps1 ($Here)"
}
$MetaPath = Join-Path $Here "metadata.json"
if (-not (Test-Path -LiteralPath $MetaPath)) {
    throw "metadata.json not found next to restore.ps1 ($Here)"
}
$meta = Get-Content -LiteralPath $MetaPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $meta.transplant) { throw "this bundle carries no cross-tool transcripts" }

# Resolve the working folder the same way convert.ps1 does.
if ($Cwd) { $work = [System.IO.Path]::GetFullPath($Cwd) }
else { $work = [System.IO.Path]::GetFullPath((Get-Location).Path) }
if (-not (Test-Path -LiteralPath $work)) {
    New-Item -ItemType Directory -Path $work -Force | Out-Null
}
$trimmed = $work.TrimEnd([char[]]@("\", "/"))
if ($trimmed -match "^[A-Za-z]:$") { $work = $trimmed + "\" } else { $work = $trimmed }

$convArgs = @{ Target = $Target; Cwd = $work }
if ($Id) { $convArgs["Id"] = $Id }
if ($ClaudeHome) { $convArgs["ClaudeHome"] = $ClaudeHome }
if ($CodexHome) { $convArgs["CodexHome"] = $CodexHome }
if ($StateDb) { $convArgs["StateDb"] = $StateDb }
if ($Force) { $convArgs["Force"] = $true }
if ($DryRun) { $convArgs["DryRun"] = $true }
try {
    & $Conv @convArgs
} catch {
    Write-Host ("Restore failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

if ($DryRun) { return }
if ($Target -eq "json") { return }

$t = $meta.transplant.$Target
$sid = if ($Id -and $Target -ne "opencode") { $Id } else { [string]$t.id }
if (-not $sid) {
    Write-Warning "could not read the new session id from metadata.json"
    return
}

$tool = ""
$cmd = ""
switch ($Target) {
    "claude" {
        $tool = "claude"
        $cmd = "claude --resume `"$sid`""
    }
    "codex" {
        $tool = "codex"
        $cmd = "codex resume `"$sid`""
    }
    "opencode" {
        $tool = "opencode"
        $cmd = "opencode --session `"$sid`""
    }
    "gemini" {
        $tool = "gemini"
        $cmd = "gemini --resume `"$sid`""
    }
    "qwen" {
        $tool = "qwen"
        $cmd = "qwen --resume `"$sid`""
    }
    "goose" {
        $tool = "goose"
        $cmd = "goose session --resume --name `"$sid`""
    }
    "cline" {
        $tool = ""
        $cmd = "Open VS Code -> Cline -> History"
    }
    "roo" {
        $tool = ""
        $cmd = "Open VS Code -> Roo Code -> History"
    }
}

Write-Host ""
Write-Host ("Session id: " + $sid)
Write-Host ("Working folder: " + $work)
Write-Host ("Resume with: " + $cmd)
if ($NoLaunch) {
    Write-Host "Re-run without -NoLaunch to open it automatically."
    return
}
if (-not $tool) {
    Write-Host "The transcript is installed; open the extension history list to continue."
    return
}

if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    Write-Warning ("$tool is not on PATH; run the command above manually")
    return
}
$shell = Get-Command "pwsh.exe" -ErrorAction SilentlyContinue
if (-not $shell) { $shell = Get-Command "powershell.exe" -ErrorAction SilentlyContinue }
if (-not $shell) { $shell = Get-Command "pwsh" -ErrorAction SilentlyContinue }
if (-not $shell) { $shell = Get-Command "powershell" -ErrorAction SilentlyContinue }
if (-not $shell) {
    Write-Warning "PowerShell not found; run the command above manually"
    return
}

# Use a small launcher file instead of -Command so paths with spaces and
# non-ASCII characters survive intact in Windows PowerShell 5.1.
$quoted = $work.Replace("'", "''")
$body = "Set-Location -LiteralPath '" + $quoted + "'`r`n" + $cmd + "`r`n"
$launcher = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("adm-restore-" + [guid]::NewGuid().ToString("N") + ".ps1")
[System.IO.File]::WriteAllText(
    $launcher, $body, (New-Object System.Text.UTF8Encoding($true)))
$argLine = '-NoExit -ExecutionPolicy Bypass -File "' + $launcher + '"'
Start-Process -FilePath $shell.Source -ArgumentList $argLine `
    -WorkingDirectory $work | Out-Null
Write-Host "Opened a new terminal with the resumed session."
'''


RESTORE_BAT = r'''@echo off
rem Restore this session into another AI tool and continue it.
rem Usage: restore.bat [claude^|codex^|opencode^|gemini^|qwen^|goose^|cline^|roo^|json] ["working folder"]
rem                    [--no-launch] [--force] [--dry-run]
rem        restore.bat            (interactive menu)
setlocal EnableExtensions
set "HERE=%~dp0"
if "%~1"=="" goto :menu
if /I "%~1"=="--help" goto :usage
if /I "%~1"=="-h" goto :usage
set "TARGET=%~1"
if /I "%TARGET%"=="claude" goto :run
if /I "%TARGET%"=="codex" goto :run
if /I "%TARGET%"=="opencode" goto :run
if /I "%TARGET%"=="gemini" goto :run
if /I "%TARGET%"=="qwen" goto :run
if /I "%TARGET%"=="goose" goto :run
if /I "%TARGET%"=="cline" goto :run
if /I "%TARGET%"=="roo" goto :run
if /I "%TARGET%"=="json" goto :run
goto :usage

:run
shift
set "WORK="
set "NO="
set "FORCE="
set "DRY="
:parse
if "%~1"=="" goto :parsed
if /I "%~1"=="--no-launch" (
    set "NO=-NoLaunch"
    shift
    goto :parse
)
if /I "%~1"=="--force" (
    set "FORCE=-Force"
    shift
    goto :parse
)
if /I "%~1"=="--dry-run" (
    set "DRY=-DryRun"
    shift
    goto :parse
)
if /I "%~1"=="--help" goto :usage
if /I "%~1"=="-h" goto :usage
set "ARG=%~1"
if "%ARG:~0,2%"=="--" goto :usage
if defined WORK goto :usage
set "WORK=%~1"
shift
goto :parse

:parsed
if not defined WORK (
    set "WORK=%CD%"
    set /p "WORK=Target working folder [%CD%]: "
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%restore.ps1" -Target "%TARGET%" -Cwd "%WORK%" %NO% %FORCE% %DRY%
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Restore failed with exit code %RC%.
exit /b %RC%

:menu
echo Restore this session into:
echo   1. Claude Code
echo   2. Codex
echo   3. OpenCode
echo   4. DeepSeek CLI
echo   5. Qwen Code
echo   6. Goose
echo   7. Cline (VS Code)
echo   8. Roo Code (VS Code)
echo   9. Generic JSON / Markdown (any other AI tool)
echo.
set "TARGET="
set "SEL="
set /p "SEL=Select [1-9]: "
if not defined SEL (
    echo.
    echo Cancelled.
    exit /b 2
)
if "%SEL%"=="1" set "TARGET=claude"
if "%SEL%"=="2" set "TARGET=codex"
if "%SEL%"=="3" set "TARGET=opencode"
if "%SEL%"=="4" set "TARGET=gemini"
if "%SEL%"=="5" set "TARGET=qwen"
if "%SEL%"=="6" set "TARGET=goose"
if "%SEL%"=="7" set "TARGET=cline"
if "%SEL%"=="8" set "TARGET=roo"
if "%SEL%"=="9" set "TARGET=json"
if not defined TARGET (
    echo Invalid selection.
    exit /b 2
)
set "WORK=%CD%"
set /p "WORK=Target working folder [%CD%]: "
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%restore.ps1" -Target "%TARGET%" -Cwd "%WORK%"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Restore failed with exit code %RC%.
pause
exit /b %RC%

:usage
echo usage: restore.bat claude^|codex^|opencode^|gemini^|qwen^|goose^|cline^|roo^|json ["working folder"] [--no-launch] [--force] [--dry-run]
echo        restore.bat            (interactive menu)
exit /b 2
'''


RESTORE_SH = r'''#!/bin/sh
# Restore this session into another AI tool and continue it.
# Usage:
#   sh restore.sh claude|codex|opencode|gemini|qwen|goose|cline|roo|json [WORKDIR] [--no-launch] [--force] [--dry-run]
#   sh restore.sh                     (interactive menu)
set -u

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
TARGET="${1:-}"

if [ -z "$TARGET" ]; then
    echo "Restore this session into:"
    echo "  1. Claude Code"
    echo "  2. Codex"
    echo "  3. OpenCode"
    echo "  4. DeepSeek CLI"
    echo "  5. Qwen Code"
    echo "  6. Goose"
    echo "  7. Cline (VS Code)"
    echo "  8. Roo Code (VS Code)"
    echo "  9. Generic JSON / Markdown (any other AI tool)"
    printf "Select [1-9]: "
    if ! read -r SEL; then exit 2; fi
    case "$SEL" in
        1) TARGET=claude ;;
        2) TARGET=codex ;;
        3) TARGET=opencode ;;
        4) TARGET=gemini ;;
        5) TARGET=qwen ;;
        6) TARGET=goose ;;
        7) TARGET=cline ;;
        8) TARGET=roo ;;
        9) TARGET=json ;;
        *) echo "invalid selection" >&2; exit 2 ;;
    esac
else
    shift
fi

usage() {
    echo "usage: sh restore.sh claude|codex|opencode|gemini|qwen|goose|cline|roo|json [WORKDIR] [--no-launch] [--force] [--dry-run]" >&2
}

case "$TARGET" in
    claude|codex|opencode|gemini|qwen|goose|cline|roo|json) ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
esac

CWD="$PWD"
NO=0
FORCE=0
DRY=0
SEEN=0
for arg in "$@"; do
    case "$arg" in
        --no-launch) NO=1 ;;
        --force) FORCE=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) usage; exit 0 ;;
        --*) echo "unknown option: $arg" >&2; exit 2 ;;
        *)
            if [ "$SEEN" = 1 ]; then
                echo "only one working folder is allowed" >&2
                exit 2
            fi
            CWD="$arg"
            SEEN=1
            ;;
    esac
done

if [ -z "$CWD" ]; then
    echo "invalid working folder" >&2
    exit 2
fi
mkdir -p "$CWD" || exit 1
CONV_ARGS=""
[ "$FORCE" = 1 ] && CONV_ARGS="$CONV_ARGS --force"
[ "$DRY" = 1 ] && CONV_ARGS="$CONV_ARGS --dry-run"
# shellcheck disable=SC2086
sh "$HERE/convert.sh" "$TARGET" "$CWD" $CONV_ARGS || exit $?

if [ "$TARGET" = "json" ]; then exit 0; fi
if [ "$DRY" = 1 ]; then exit 0; fi

tp_id() {
    sed -n "/\"$1\"[[:space:]]*:/,/}/p" "$HERE/metadata.json" 2>/dev/null \
        | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -n 1
}
SID=$(tp_id "$TARGET")
if [ -z "$SID" ]; then
    echo "warning: could not read the new session id from metadata.json" >&2
    exit 1
fi

echo ""
echo "Session id: $SID"
echo "Working folder: $CWD"
case "$TARGET" in
    claude) CMD="claude --resume \"$SID\"" ;;
    codex) CMD="codex resume \"$SID\"" ;;
    opencode) CMD="opencode --session \"$SID\"" ;;
    gemini) CMD="gemini --resume \"$SID\"" ;;
    qwen) CMD="qwen --resume \"$SID\"" ;;
    goose) CMD="goose session --resume --name \"$SID\"" ;;
    cline) CMD="open VS Code -> Cline -> History" ;;
    roo) CMD="open VS Code -> Roo Code -> History" ;;
esac
echo "Resume with: $CMD"
if [ "$NO" = 1 ]; then exit 0; fi
case "$TARGET" in
    cline|roo)
        echo "The transcript is installed; open the extension history list to continue."
        exit 0
        ;;
esac

case "$TARGET" in
    claude) TOOL=claude ;;
    codex) TOOL=codex ;;
    opencode) TOOL=opencode ;;
    gemini) TOOL=gemini ;;
    qwen) TOOL=qwen ;;
    goose) TOOL=goose ;;
esac
if ! command -v "$TOOL" >/dev/null 2>&1; then
    echo "warning: $TOOL is not on PATH; run the command above manually" >&2
    exit 1
fi

cd "$CWD" || exit 1
case "$TARGET" in
    claude) exec claude --resume "$SID" ;;
    codex) exec codex resume "$SID" ;;
    opencode) exec opencode --session "$SID" ;;
    gemini) exec gemini --resume "$SID" ;;
    qwen) exec qwen --resume "$SID" ;;
    goose) exec goose session --resume --name "$SID" ;;
esac
'''


RESTORE_TARGET_BAT = r'''@echo off
rem Restore this session into __LABEL__ and continue it.
rem Usage: restore-__TARGET__.bat ["working folder"] [--no-launch] [--force] [--dry-run]
setlocal
call "%~dp0restore.bat" __TARGET__ %*
set "RC=%ERRORLEVEL%"
rem Pause only when double-clicked (cmdcmdline contains this script name).
echo "%cmdcmdline%" | find /I "%~nx0" >nul
if not errorlevel 1 pause
exit /b %RC%
'''


RESTORE_TARGET_PS1 = r'''# Restore this session into __LABEL__ and continue it.
# Usage:
#   powershell -ExecutionPolicy Bypass -File restore-__TARGET__.ps1
#       ["WORKDIR"] [-NoLaunch] [-Force] [-DryRun]
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Cwd = "",
    [switch]$NoLaunch,
    [switch]$Force,
    [switch]$DryRun
)
$Here = $PSScriptRoot
if (-not $Here) { $Here = Split-Path -Parent $MyInvocation.MyCommand.Path }
& (Join-Path $Here "restore.ps1") -Target __TARGET__ -Cwd $Cwd `
    -NoLaunch:$NoLaunch -Force:$Force -DryRun:$DryRun
'''


RESTORE_TARGET_SH = r'''#!/bin/sh
# Restore this session into __LABEL__ and continue it.
# Usage: sh restore-__TARGET__.sh ["WORKDIR"] [--no-launch] [--force] [--dry-run]
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec sh "$HERE/restore.sh" __TARGET__ "$@"
'''
