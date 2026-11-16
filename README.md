# AI Dialog Manager

PyQt6 桌面应用:统一浏览 **Claude Code / Codex / OpenCode / Junie / Pi / Gemini CLI /
Qwen Code / Goose / Cline / Roo Code / Qoder** 的会话 —— 搜索、收藏、分组、导出、
一键恢复;Claude/Codex 还可编辑聊天记录与追加消息。
「意识上传 / 跨工具适配」可把一个会话**转生到另一种 AI 工具**,并指定新的工作目录。

## 运行

```powershell
uv sync                # 安装依赖 (PyQt6)
uv run adm             # 启动 GUI
```

已打包单文件 exe:`dist\AI-Dialog-Manager.exe`(双击即可,无需 Python)。

重新打包:

```powershell
uv run pyinstaller --noconfirm --onefile --windowed --name AI-Dialog-Manager run.py
```

调试模式:

```powershell
uv run adm --selftest   # 无界面自检:扫描/解析/编辑回环/删链修复
uv run adm --repair     # 修复断链的 Claude 会话(parentUuid/leafUuid 悬空),自动备份,跳过运行中会话
$env:ADM_AUTOTEST='1'; uv run adm --smoke   # GUI 自动化深度测试

# 跨工具转移(脚本方式):id 可来自任意来源(Claude/Codex/OpenCode/Junie/Pi/...)
uv run adm --transplant <会话id或id前缀> --to codex --cwd D:\hello
uv run adm --transplant <会话id> --to opencode --cwd D:\hello
uv run adm --transplant <会话id> --to json --out D:\transcripts
uv run adm --transplant path\to\session.jsonl --to claude --cwd D:\hello
# 也支持 OpenCode 导出的 JSON(见下方“跨工具适配”)
uv run adm --transplant opencode-export.json --to codex --cwd D:\hello --title "新标题"
```

## 功能

| 功能 | 说明 |
|---|---|
| 统一会话列表 | Claude、Codex、OpenCode、Junie、Pi、Gemini、Qwen、Goose、Cline、Roo、Qoder 汇成一个列表;列:收藏/来源/标题/项目/消息数/大小/tokens/时间/状态。Claude/Codex 可编辑,其它来源只读 |
| **增量扫描缓存** | 首次扫描建立 `%APPDATA%\ai-dialog-manager\scan_cache.json`;之后启动只 stat 变更文件,821 个会话约 0.7 秒(实测)。缓存损坏会自动忽略并重扫 |
| **中英双语** | 工具栏右侧 🌐 下拉切换 中文 / English,即时生效全界面(含菜单、对话框、表头),选择自动记住(下次启动沿用) |
| 筛选 | 关键词、来源、项目下拉、只看收藏、隐藏归档、隐藏子代理 |
| 排序 | 排序下拉(更新/创建时间、标题、项目、大小、消息数)+ ↑↓ 方向切换;表头点击排序仍可用 |
| 按项目分组 | 「按项目分组」按钮切换成树视图:项目节点(会话数/消息数/总大小/最近更新)→ 会话;分组内同样支持多选与右键菜单 |
| **批处理** | Ctrl/Shift 多选会话 → 工具栏「⋯ 批处理」:批量收藏/取消、归档(仅 Codex)、导出 Markdown 到文件夹、删除(均自动备份) |
| 聊天记录查看 | 用户/助手气泡、thinking/工具调用/工具结果折叠块、时间戳、模型;会话内过滤(只看用户/助手/无工具噪音)+ 会话内搜索;大文件分页渲染;**「倒序」开关**(最新消息在前,「加载更多」翻向更早);选择自动记住 |
| **会话内批量操作** | 聊天视图「批量选择」:勾选多条消息 → 批量删除(一次写入、自动接链)/ 复制 / 导出 Markdown;支持全选可见、清除选择 |
| **编辑聊天记录** | 双击消息 → 文本编辑 / Raw JSON 两个标签;删除消息自动连带其 tool_use/tool_result 配对,并**自动接回 parentUuid 链**(`uv run adm --repair` 可修历史断链文件);删除后**自动跳回原位置**并自动展开分页,不再回到顶部;追加用户消息(恢复后 CLI 可见) |
| 重命名 | Claude:追加 ai-title 记录;Codex:更新 state_5.sqlite |
| 收藏/备注 | 本地 sidecar 存储,永不触碰原始文件 |
| 全局搜索 | 后台线程全文检索所有会话(含工具参数),双击结果跳转到具体消息 |
| 一键恢复 | 新终端窗口在项目目录运行 `claude --resume` / `codex resume` / `opencode --session` / `junie --session-id ... --resume` / `pi --session` / `gemini` / `qwen` / `goose`;Cline/Roo/Qoder 为 GUI-only,会提示在对应应用打开 |
| 归档/复制/删除 | Codex sqlite 归档位;会话复制成新 id;删除前自动备份 |
| **意识上传** | 把 Claude / Codex 会话导出成一个自包含的上传包:`session.jsonl` + `metadata.json` + `install.bat` / `install.ps1` / `install.sh` / `install.py`(同工具恢复)+ `convert.bat` / `convert.ps1` / `convert.sh` / `convert.py`(换工具)+ `restore-claude.*` / `restore-codex.*` / `restore-opencode.*` / `restore-gemini.*` / `restore-qwen.*` / `restore-goose.*` / `restore-cline.*` / `restore-roo.*` / `restore-json.*`(换工具并直接继续)+ `transplant/`(换 AI 工具的目标记录)+ `README.md`,Claude 还含 `subagents/`。**同工具**:Windows 双击 `install.bat`,Linux/macOS 运行 `sh install.sh "<工作文件夹>"`,写入对方会话库(新 id、cwd 改写,Codex 自动登记 sqlite),然后 `/resume`。**换工具**:`restore-codex.bat "D:\hello"` / `sh restore-opencode.sh "/home/x"` 会转换并直接打开目标工具的新会话;也可用 `convert.bat codex "D:\hello"` 只转换。目标可选 `claude` / `codex` / `opencode` / `gemini` / `qwen` / `goose` / `cline` / `roo` / `json`,工作文件夹可与原始目录完全不同(例:原 `E:\test` 的 Claude 会话 → `D:\hello` 的 Codex 会话)。Claude 路径无需 Python;Codex 无 Python 时用 `sqlite3`,再没有则打印可执行的 SQL。支持多选批量上传 |
| **跨工具适配(转生)** | 工具栏「⤴ 适配其它 AI」:把任意来源会话转换成 **Claude Code / Codex / OpenCode / Gemini CLI / Qwen Code / Goose / Cline / Roo Code / 通用 JSON+Markdown**,可指定新的工作目录(例如 `E:\test` → `D:\hello`)。新会话使用全新 id,原会话不改动;工具调用/结果(可选思考、子代理)会转成可读文本,避免目标工具不认识源工具 schema。支持批量。脚本入口:`uv run adm --transplant ...` |
| 统计 | 总览 + 按项目聚合表 |
| 导出 | 会话导出为 Markdown |

## 会话来源

| 来源 | 读取位置 | 编辑 | 继续 |
|---|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | ✅ | `claude --resume <id>` |
| Codex | `~/.codex/state_*.sqlite` + `sessions/**/rollout-*.jsonl` | ✅ | `codex resume <id>` |
| OpenCode | `~/.local/share/opencode/opencode.db`(`session_v2` / `session_message`) | 只读 | `opencode --session <id>` |
| Junie | `~/.junie/sessions/<id>/transcript.md` + `summary.json` | 只读 | `junie --session-id <id> --resume` |
| Pi | `~/.pi/agent/sessions/<project>/*.jsonl` | 只读 | `pi --session <id>` |
| Gemini CLI | `~/.gemini/tmp/<项目哈希>/chats/session-*.json` | 只读 | `gemini --resume <id>` |
| Qwen Code | `~/.qwen/tmp/<项目哈希>/chats/session-*.json` | 只读 | `qwen --resume <id>` |
| Goose | `%APPDATA%\Block\goose\data\sessions\*.json` 或 `~/.local/share/goose/sessions/*.json` | 只读 | `goose session --resume --name <id>` |
| Cline | VS Code `globalStorage/saoudrizwan.claude-dev/tasks/<id>/` | 只读 | 在 VS Code Cline 历史中打开 |
| Roo Code | VS Code `globalStorage/rooveterinaryinc.roo-cline/tasks/<id>/` | 只读 | 在 VS Code Roo 历史中打开 |
| Qoder | `%APPDATA%\com.qodercn.app.stable\main.sqlite`(`chat_sessions` / `chat_session_messages`) | 只读 | 在 Qoder 中打开 |

- 只读来源支持:列表、来源/项目筛选、关键词、全局搜索、收藏/备注、自定义标题、导出 Markdown、统计、按项目分组、跨工具转生;不支持编辑/删除消息、复制会话、归档或导出「可恢复上传包」,以免误改原应用数据库。
- 来源筛选只显示本机实际存在的来源,不会堆一排空选项。
- 测试/便携环境可用环境变量覆盖位置:`OPENCODE_DATA_DIR`、`JUNIE_HOME`、`PI_AGENT_HOME`、`GEMINI_CLI_HOME`、`QWEN_HOME`、`GOOSE_HOME`、`ADM_VSCODE_GLOBAL_STORAGE`、`QODER_HOME`。

### 扫描性能

- 会话元数据缓存到 `%APPDATA%\ai-dialog-manager\scan_cache.json`,以 `(路径, mtime, 大小)` 做指纹;未变化的文件不会重新读取。
- 实测本机 821 个会话:首次扫描约 20 秒(Claude 文件多且大),之后启动约 **0.7 秒**。
- 想强制重建缓存:关闭 App,删除上述 `scan_cache.json`,重新打开。
- 新增来源/文件会被自动发现;来源筛选、项目下拉、统计会随之更新。

## 跨工具适配(意识上传 · 转生)

两种方式,结果一致:

1. **App 内**:选中会话 → 工具栏「⤴ 适配其它 AI」或右键「适配到其它 AI 工具…」。
   选择目标工具、目标工作目录、新标题,以及是否带上工具记录/思考/子代理。
2. **上传包内**(目标机器不需要装本应用):`⤴ 意识上传` 导出后,包里已经预渲染了
   各目标的转录,直接在包目录运行:

   ```bat
   :: Windows — 转换 + 直接继续
   restore-codex.bat "D:\hello"
   restore-opencode.bat "D:\hello"
   restore-claude.bat "D:\hello"
   restore-gemini.bat "D:\hello"
   restore-qwen.bat "D:\hello"
   restore-goose.bat "D:\hello"
   restore-cline.bat "D:\hello"
   restore-roo.bat "D:\hello"
   restore-json.bat "D:\hello"
   restore.bat            :: 菜单
   ```

   ```sh
   # Linux / macOS
   sh restore-codex.sh "/home/user/hello"
   sh restore-opencode.sh "/home/user/hello"
   sh restore-claude.sh "/home/user/hello"
   sh restore-gemini.sh "/home/user/hello"
   sh restore-qwen.sh "/home/user/hello"
   sh restore-goose.sh "/home/user/hello"
   sh restore-json.sh "/home/user/hello"
   ```

   每个 `restore-*` 脚本先调用同目录的 `convert.*` 安装记录,再直接打开目标工具并
   定位到新会话(`codex resume <id>` / `opencode --session <id>` /
   `claude --resume <id>` / `gemini --resume <id>` / `qwen --resume <id>` /
   `goose session --resume --name <id>`)。Cline / Roo Code 只安装到 VS Code
   扩展的任务目录,在扩展历史列表里打开。加 `--no-launch` 只安装、不打开;加
   `--force` 覆盖包内固定的目标 id。只想手动转换时仍可用:

   ```bat
   convert.bat codex "D:\hello"
   convert.bat gemini "D:\hello"
   ```

   ```sh
   sh convert.sh claude "/home/user/hello"
   sh convert.sh goose "/home/user/hello"
   ```

   支持 `claude` / `codex` / `opencode` / `gemini` / `qwen` / `goose` / `cline` /
   `roo` / `json`;另有 `convert.ps1` / `convert.py` 和 `restore-claude.ps1` 等
   PowerShell 版本。工作目录可以和原始目录完全不同(例:原 `E:\test` → 新
   `D:\hello`),不需要 Python 也能完成 Claude 与通用 JSON 两条路径;新增目标
   (`gemini` / `qwen` / `goose` / `cline` / `roo`)的安装步骤需要 Python 3。

| 目标 | 写入位置 | 一键恢复脚本 | 继续方式 |
|---|---|---|---|
| Claude Code | `~/.claude/projects/<新目录转义>/<新uuid>.jsonl` | `restore-claude.bat/.sh` | `claude --resume <id>` |
| Codex | `~/.codex/sessions/年/月/日/rollout-*.jsonl` + `state_5.sqlite` 登记 | `restore-codex.bat/.sh` | `codex resume <id>` |
| OpenCode (2.x) | 通过本机 OpenCode 服务的 `/api/experimental/session/import` 导入 | `restore-opencode.bat/.sh` | `opencode --session <id>` |
| Gemini CLI | `~/.gemini/tmp/<sha256(目录)>/chats/session-*.json` | `restore-gemini.bat/.sh` | `gemini --resume <id>` |
| Qwen Code | `~/.qwen/tmp/<sha256(目录)>/chats/session-*.json` | `restore-qwen.bat/.sh` | `qwen --resume <id>` |
| Goose | `~/.local/share/goose/sessions/<id>.json`(Windows `%APPDATA%\Block\goose\data\sessions`) | `restore-goose.bat/.sh` | `goose session --resume --name <id>` |
| Cline | VS Code `globalStorage/saoudrizwan.claude-dev/tasks/<id>/` | `restore-cline.bat/.sh` | 打开 VS Code → Cline → 历史 |
| Roo Code | VS Code `globalStorage/rooveterinaryinc.roo-cline/tasks/<id>/` | `restore-roo.bat/.sh` | 打开 VS Code → Roo Code → 历史 |
| 通用 / 其它 AI 工具 | `<目录>/<标题>-<id8>/transcript.json` + `transcript.md` + `README.md` | `restore-json.bat/.sh` | 交给任意工具/代理继续 |

- **为什么把工具调用变成文本**:目标工具的 function/tool schema 与源工具不同,原样保留会让恢复后的下一次模型请求报“未知工具/工具结果不匹配”。转成 `[tool: name] … [tool result] …` 文本最稳,也保留决策上下文。
- **不会再把整段对话合并成一块**:转换按源消息的文本/思考/工具调用/工具结果拆成独立消息;一次工具调用和它的结果放在同一条里,其余分段。这样目标工具里是多个气泡,而不是一个几百 KB 的大消息。
- OpenCode 目标需要本机 `opencode` 可用(会调用 `opencode api get /api/info` 找服务,读取 `~/.config/opencode/service.json` 的本地密码);导入后立即出现在目标目录的会话列表,无需重启。
- **OpenCode → 其它工具**:用 OpenCode 自己的导出接口得到 JSON,再交给本工具:

  ```powershell
  opencode api get /api/experimental/session/<sessionID>/export | Out-File -Encoding utf8 session.json
  uv run adm --transplant session.json --to claude --cwd D:\hello
  ```

  这条链路让 Claude / Codex / OpenCode 之间可以任意方向迁移(通用 JSON 也可作为中间格式)。
- 默认跳过 Claude 子代理记录、思考块和 CLI 注入的 `<command-name>` / `<system-reminder>` 等噪音,避免污染目标会话;对话框里可以打开思考/子代理。

## 数据与安全

- **只读优先**:列表扫描不写任何原始文件。
- **每次写之前自动备份**:`%APPDATA%\ai-dialog-manager\backups\<source>\<sessionId>\`,保留最近 8 份。
- **外部修改检测**:保存时校验文件快照;若 CLI 在你打开期间追加了内容,新行会被保留合并,已变动的旧行会拒绝覆盖并提示。
- **原子写入**:临时文件 + `os.replace`。
- Codex 编辑后会清掉 `thread_history_1.sqlite` 里该线程的投影缓存,让 Codex 从 rollout 文件重新投影;建议编辑 Codex 会话前先关掉正在跑的 codex 进程。

## 数据源

| | Claude Code | Codex |
|---|---|---|
| 会话文件 | `~/.claude/projects/<路径转义>/<sessionId>.jsonl` | `~/.codex/sessions/年/月/日/rollout-*.jsonl` |
| 元数据 | jsonl 内 ai-title / 首条用户消息 | `~/.codex/state_5.sqlite` threads 表 |
| 运行中判定 | `~/.claude/sessions/<pid>.json` + pid 存活 | 文件 3 分钟内被写过 / 写锁 |
| 子代理 | `projects/<proj>/<sessionId>/subagents/*.jsonl` | — |

## 结构

```
adm/
├── config.py        路径常量
├── models.py        SessionMeta / Message / Part
├── jsonlio.py       行级编辑器:备份 + 外部修改检测 + 尾部合并 + 原子写
├── claude_store.py  Claude 扫描/解析/编辑/追加
├── codex_store.py   Codex sqlite + rollout 扫描/解析/编辑/追加/投影失效
├── local_store.py   收藏/备注 sidecar (APPDATA)
├── scanner.py       后台扫描线程
├── searcher.py      全局搜索线程
├── launcher.py      新终端恢复会话
├── transfer.py      意识上传:导出可恢复会话包 + 安装器模板
├── transplant.py    跨工具适配:中立会话 + Claude/Codex/OpenCode/JSON 写入器
├── (adapter)        新工具只需加一个 write_* 适配器
├── i18n.py          中英文案表 + tr() / 语言切换
└── ui/              主题 / 表格模型 / 聊天视图 / 对话框 / 主窗口
```
