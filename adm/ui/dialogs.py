"""Dialogs: message editor, add-message, global search, stats, export."""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from .. import util
from ..i18n import role_label, tr
from ..models import LoadedSession, Message, SessionMeta
from ..transplant import TransplantOptions
from ..searcher import SearchThread
from . import theme

MONO = "Cascadia Mono, Consolas, monospace"

_STD_TEXT = {
    QDialogButtonBox.StandardButton.Save: "save",
    QDialogButtonBox.StandardButton.Cancel: "cancel",
    QDialogButtonBox.StandardButton.Ok: "ok",
    QDialogButtonBox.StandardButton.Close: "close",
}


def _localize_buttons(box: QDialogButtonBox) -> None:
    for btn, key in _STD_TEXT.items():
        b = box.button(btn)
        if b:
            b.setText(tr(key))


def _mono_te() -> QPlainTextEdit:
    te = QPlainTextEdit()
    f = te.font()
    f.setFamily(MONO)
    f.setPointSize(9)
    te.setFont(f)
    te.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return te


class MessageEditDialog(QDialog):
    """Edit a message. Tab 1: text parts. Tab 2: raw record JSON."""

    def __init__(self, msg: Message, parent=None):
        super().__init__(parent)
        self.msg = msg
        self.setWindowTitle(tr("edit_dlg_title", role=role_label(msg.role),
                               line=msg.line))
        self.resize(780, 560)
        lay = QVBoxLayout(self)

        info = QLabel(f"{role_label(msg.role)} · {util.fmt_dt(msg.ts)} · {msg.model or ''}")
        info.setStyleSheet(f"color: {theme.DIM};")
        lay.addWidget(info)

        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # --- text tab
        text_page = QWidget()
        tlay = QVBoxLayout(text_page)
        self.text_edits: list[tuple[str, QPlainTextEdit]] = []
        if msg.codex_payload is not None:
            p = msg.codex_payload
            if p.get("type") == "message":
                for i, part in enumerate(p.get("content") or []):
                    if isinstance(part, dict) and part.get("type") in (
                            "input_text", "output_text", "text"):
                        tlay.addWidget(QLabel(tr("part_n", i=i, t=part["type"])))
                        te = QPlainTextEdit(part.get("text") or "")
                        te.setMinimumHeight(160)
                        tlay.addWidget(te)
                        self.text_edits.append(("text", te))
            elif p.get("type") == "function_call":
                tlay.addWidget(QLabel(tr("args_json")))
                te = _mono_te()
                args = p.get("arguments")
                te.setPlainText(args if isinstance(args, str) else
                                json.dumps(args, ensure_ascii=False, indent=2))
                tlay.addWidget(te)
                self.text_edits.append(("args", te))
            elif p.get("type") == "function_call_output":
                tlay.addWidget(QLabel(tr("output_lbl")))
                te = _mono_te()
                out = p.get("output")
                te.setPlainText(out if isinstance(out, str) else
                                json.dumps(out, ensure_ascii=False))
                tlay.addWidget(te)
                self.text_edits.append(("out", te))
        else:
            rec = msg.claude_record or {}
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str):
                te = QPlainTextEdit(content)
                te.setMinimumHeight(200)
                tlay.addWidget(te)
                self.text_edits.append(("text", te))
            elif isinstance(content, list):
                for i, part in enumerate(content):
                    if not isinstance(part, dict):
                        continue
                    pt = part.get("type")
                    if pt == "text":
                        tlay.addWidget(QLabel(tr("part_n", i=i, t=pt)))
                        te = QPlainTextEdit(part.get("text") or "")
                        tlay.addWidget(te)
                        self.text_edits.append(("text", te))
                    elif pt == "thinking":
                        tlay.addWidget(QLabel(tr("part_n", i=i, t=pt)))
                        te = QPlainTextEdit(part.get("thinking") or "")
                        tlay.addWidget(te)
                        self.text_edits.append(("think", te))
                    elif pt == "tool_use":
                        tlay.addWidget(QLabel(tr("part_n", i=i, t="tool_use input (JSON)")))
                        te = _mono_te()
                        te.setPlainText(json.dumps(part.get("input"),
                                                   ensure_ascii=False, indent=2))
                        tlay.addWidget(te)
                        self.text_edits.append(("toolinput", te))
                    elif pt == "tool_result":
                        tlay.addWidget(QLabel(tr("part_n", i=i, t="tool_result content")))
                        te = _mono_te()
                        c = part.get("content")
                        te.setPlainText(c if isinstance(c, str) else
                                        json.dumps(c, ensure_ascii=False, indent=2))
                        tlay.addWidget(te)
                        self.text_edits.append(("toolresult", te))
        if not self.text_edits:
            tlay.addWidget(QLabel(tr("no_editable")))
        tabs.addTab(text_page, tr("tab_text"))

        # --- raw tab
        raw_page = QWidget()
        rlay = QVBoxLayout(raw_page)
        self.raw_edit = _mono_te()
        src = msg.codex_payload if msg.codex_payload is not None else msg.claude_record
        if msg.codex_payload is not None:
            src = json.loads(json.dumps(src))
        self.raw_edit.setPlainText(json.dumps(src, ensure_ascii=False, indent=2))
        self.raw_edit.setMinimumHeight(300)
        rlay.addWidget(self.raw_edit)
        self.raw_err = QLabel("")
        self.raw_err.setStyleSheet("color: #e06c75;")
        rlay.addWidget(self.raw_err)
        tabs.addTab(raw_page, tr("tab_raw"))

        self.tabs = tabs
        self.use_raw = False
        tabs.currentChanged.connect(lambda i: setattr(self, "use_raw", i == 1))

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                QDialogButtonBox.StandardButton.Cancel)
        _localize_buttons(btns)
        btns.accepted.connect(self._validate)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _validate(self) -> None:
        if self.use_raw:
            try:
                json.loads(self.raw_edit.toPlainText())
            except ValueError as e:
                self.raw_err.setText(tr("invalid_json", e=e))
                return
        self.accept()

    def new_texts(self) -> dict[int, str]:
        """{part index within msg.parts -> new text} for store.apply_text_edits."""
        out: dict[int, str] = {}
        # build mapping mirroring the dialog construction order
        if self.msg.codex_payload is not None:
            return {i: te.toPlainText() for i, (_, te) in enumerate(self.text_edits)}
        rec = self.msg.claude_record or {}
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            return {0: self.text_edits[0][1].toPlainText()} if self.text_edits else {}
        idx = 0
        for kind, te in self.text_edits:
            out[idx] = te.toPlainText()
            idx += 1
        return out

    def raw_json(self) -> str:
        return self.raw_edit.toPlainText()


class AddMessageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("add_title"))
        self.resize(640, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("add_desc")))
        self.te = QPlainTextEdit()
        self.te.setPlaceholderText(tr("add_ph"))
        lay.addWidget(self.te)
        self.warn = QLabel("")
        self.warn.setStyleSheet(f"color: {theme.WARN};")
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        _localize_buttons(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def text(self) -> str:
        return self.te.toPlainText().strip()


class GlobalSearchDialog(QDialog):
    open_hit = pyqtSignal(object, int)   # SessionMeta, line

    def __init__(self, metas: list[SessionMeta], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("gs_title"))
        self.resize(960, 640)
        self.thread: SearchThread | None = None
        self.hits: list[tuple[SessionMeta, int]] = []
        self.metas: list[SessionMeta] = metas

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(tr("gs_ph"))
        self.query.returnPressed.connect(self.start)
        top.addWidget(self.query, 1)
        self.btn = QPushButton(tr("gs_search"))
        self.btn.clicked.connect(self.start)
        top.addWidget(self.btn)
        self.stop = QPushButton(tr("gs_stop"))
        self.stop.clicked.connect(lambda: self.thread and self.thread.cancel())
        top.addWidget(self.stop)
        lay.addLayout(top)

        self.status = QLabel(tr("gs_idle"))
        self.status.setStyleSheet(f"color: {theme.DIM};")
        lay.addWidget(self.status)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            [tr("gs_col_source"), tr("gs_col_session"),
             tr("gs_col_role"), tr("gs_col_snippet")])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.doubleClicked.connect(self._open)
        lay.addWidget(self.table, 1)

    def start(self) -> None:
        q = self.query.text().strip()
        if not q or not self.metas:
            return
        if self.thread and self.thread.isRunning():
            self.thread.cancel()
            self.thread.wait(2000)
        self.table.setRowCount(0)
        self.hits.clear()
        self.thread = SearchThread(self.metas, q, self)
        self.thread.hit.connect(self._add_hit)
        self.thread.status.connect(self.status.setText)
        self.thread.done.connect(self._done)
        self.thread.start()

    def _add_hit(self, h: dict) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        m: SessionMeta = h["meta"]
        vals = [m.source_badge, m.display_title, h["role"], h["snippet"]]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            it.setToolTip(f"{h['text'][:600]}\n\n{m.cwd or m.path}")
            self.table.setItem(r, c, it)
        self.hits.append((m, h["line"]))
        self.table.scrollToBottom()

    def _done(self, n: int) -> None:
        self.status.setText(tr("gs_done", n=n))

    def _open(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.hits):
            meta, line = self.hits[r]
            self.open_hit.emit(meta, line)

    def closeEvent(self, e: QCloseEvent) -> None:
        if self.thread and self.thread.isRunning():
            self.thread.cancel()
            self.thread.wait(2000)
        super().closeEvent(e)


class StatsDialog(QDialog):
    def __init__(self, metas: list[SessionMeta], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("stats_title"))
        self.resize(760, 560)
        lay = QVBoxLayout(self)

        cl = [m for m in metas if m.source == "claude"]
        cx = [m for m in metas if m.source == "codex"]
        total_size = sum(m.size for m in metas)
        total_msg = sum(m.n_messages for m in metas)
        total_tok = sum(m.tokens or 0 for m in metas)

        from .. import sources
        others: dict[str, int] = {}
        for m in metas:
            if m.source not in ("claude", "codex"):
                others[m.source] = others.get(m.source, 0) + 1
        extra = " · ".join(f"{sources.badge(k)} {v}"
                           for k, v in sorted(others.items(), key=lambda kv: -kv[1]))
        head = QLabel(
            tr("stats_l1", n=len(metas), m=total_msg, s=util.fmt_size(total_size),
               t=util.fmt_tokens(total_tok or None) or "–") + "<br>" +
            tr("stats_l2", c=len(cl), x=len(cx),
               s=sum(1 for m in metas if m.starred),
               r=sum(1 for m in metas if m.running)) +
            (("<br>" + extra) if extra else ""))
        head.setStyleSheet(f"color: {theme.TEXT}; font-size: 14px;")
        lay.addWidget(head)

        by_proj: dict[str, list[int]] = {}
        for m in metas:
            e = by_proj.setdefault(m.project, [0, 0, 0, 0])
            e[0] += 1
            e[1] += m.n_messages
            e[2] += m.size
            e[3] += m.tokens or 0
        rows = sorted(by_proj.items(), key=lambda kv: kv[1][2], reverse=True)
        t = QTableWidget(len(rows), 4)
        t.setHorizontalHeaderLabels(
            [tr("stats_col_proj"), tr("stats_col_sessions"),
             tr("stats_col_msgs"), tr("stats_col_size")])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i, (proj, (n, msgs, size, toks)) in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(proj))
            t.setItem(i, 1, QTableWidgetItem(str(n)))
            t.setItem(i, 2, QTableWidgetItem(f"{msgs:,}"))
            t.setItem(i, 3, QTableWidgetItem(
                f"{util.fmt_size(size)}" + (f" · {util.fmt_tokens(toks)}" if toks else "")))
        lay.addWidget(t, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        _localize_buttons(btns)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        lay.addWidget(btns)


class TransplantDialog(QDialog):
    """Pick a target AI tool / working directory for a cross-tool transplant."""

    def __init__(self, meta: SessionMeta, parent=None, *,
                 last_target: str = "codex", last_cwd: str = ""):
        super().__init__(parent)
        self.meta = meta
        self.setWindowTitle(tr("tp_title"))
        self.resize(680, 460)
        lay = QVBoxLayout(self)

        src = QLabel(tr("tp_src", title=meta.display_title,
                        source=meta.source_badge, n=meta.n_messages,
                        cwd=meta.cwd or meta.project or "?"))
        src.setWordWrap(True)
        src.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(src)

        form = QFormLayout()
        self.target = QComboBox()
        for key in ("claude", "codex", "opencode", "gemini", "qwen", "goose",
                    "cline", "roo", "json"):
            self.target.addItem(tr("tp_" + key), key)
        idx = self.target.findData(last_target)
        if idx >= 0:
            self.target.setCurrentIndex(idx)
        form.addRow(tr("tp_target"), self.target)

        cwd_row = QHBoxLayout()
        self.cwd_edit = QLineEdit(last_cwd or meta.cwd or str(Path.home()))
        browse = QPushButton(tr("tp_browse"))
        browse.clicked.connect(self._browse)
        cwd_row.addWidget(self.cwd_edit, 1)
        cwd_row.addWidget(browse)
        cwd_box = QWidget()
        cwd_box.setLayout(cwd_row)
        form.addRow(tr("tp_cwd"), cwd_box)

        self.title_edit = QLineEdit(meta.display_title)
        form.addRow(tr("tp_name"), self.title_edit)
        lay.addLayout(form)

        self.cb_tools = QCheckBox(tr("tp_tools"))
        self.cb_tools.setChecked(True)
        self.cb_reasoning = QCheckBox(tr("tp_reasoning"))
        self.cb_sidechain = QCheckBox(tr("tp_sidechain"))
        lay.addWidget(self.cb_tools)
        lay.addWidget(self.cb_reasoning)
        lay.addWidget(self.cb_sidechain)

        hint = QLabel(tr("tp_cwd_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.DIM};")
        lay.addWidget(hint)
        note = QLabel(tr("tp_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.DIM};")
        lay.addWidget(note)
        lay.addStretch(1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                | QDialogButtonBox.StandardButton.Cancel)
        _localize_buttons(btns)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _browse(self) -> None:
        start = self.cwd_edit.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, tr("tp_pick"), start)
        if d:
            self.cwd_edit.setText(d)

    def _accept(self) -> None:
        cwd = self.cwd_edit.text().strip()
        if not cwd:
            QMessageBox.warning(self, tr("tp_title"), tr("tp_empty_cwd"))
            return
        p = Path(cwd).expanduser()
        if not p.is_dir():
            box = QMessageBox(self)
            box.setWindowTitle(tr("tp_mkdir_title"))
            box.setText(tr("tp_mkdir_body", path=str(p)))
            box.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, tr("tp_failed"), str(e))
                return
        self.accept()

    def values(self):
        opt = TransplantOptions(
            include_tools=self.cb_tools.isChecked(),
            include_reasoning=self.cb_reasoning.isChecked(),
            include_sidechain=self.cb_sidechain.isChecked(),
        )
        return (self.target.currentData(), self.cwd_edit.text().strip(),
                self.title_edit.text().strip(), opt)


def export_markdown(session: LoadedSession, path: str) -> None:
    m = session.meta
    lines = [f"# {m.display_title}", ""]
    lines.append(f"- source: {m.source_badge}")
    lines.append(f"- project: `{m.cwd or m.project}`")
    lines.append(f"- created: {util.fmt_dt(m.created)} · updated: {util.fmt_dt(m.updated)}")
    lines.append(f"- id: `{m.id}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    for msg in session.messages:
        ts = util.fmt_dt(msg.ts) if msg.ts else ""
        head = f"## {msg.role}" + (f" · {ts}" if ts else "")
        lines.append(head)
        lines.append("")
        for p in msg.parts:
            if p.kind == "text":
                lines.append(p.text)
                lines.append("")
            elif p.kind in ("thinking", "reasoning"):
                lines.append(f"> thinking: {p.text[:2000]}")
                lines.append("")
            elif p.kind == "tool_call":
                lines.append(f"```json\n// tool call: {p.name}\n{p.text}\n```")
                lines.append("")
            elif p.kind == "tool_result":
                lines.append("```\n" + p.text[:4000] + "\n```")
                lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip() + "\n")
