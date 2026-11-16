"""Session viewer: renders the message stream with collapsible tool blocks,
chunked loading for huge sessions, in-session filtering and edit hooks."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from .. import util
from ..i18n import role_label, tr
from ..models import LoadedSession, Message
from . import theme

PAGE = 150
TEXT_LIMIT = 6000

MONO = "Cascadia Mono, Consolas, monospace"

# filter combo: (data, key)
FILTER_MODES = [
    ("all", "cf_all"), ("user", "cf_user"), ("asst", "cf_asst"),
    ("notool", "cf_notool"), ("text", "cf_text"),
]


class Collapsible(QWidget):
    def __init__(self, title: str, body: QWidget, start_open: bool = False,
                 color: str = theme.TOOL):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.btn = QToolButton()
        self.btn.setText(("▾ " if start_open else "▸ ") + title)
        self.btn.setStyleSheet(
            f"QToolButton {{ color: {color}; background: transparent; border: 0;"
            f" padding: 2px 2px; font-size: 12px; }}")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setCheckable(True)
        self.btn.setChecked(start_open)
        self.body = body
        body.setVisible(start_open)
        self.btn.toggled.connect(self._toggle)
        lay.addWidget(self.btn)
        lay.addWidget(body)

    def _toggle(self, on: bool) -> None:
        self.body.setVisible(on)
        self.btn.setText(("▾ " if on else "▸ ") + self.btn.text()[2:])


def _long_label(text: str, mono: bool = False, color: str = theme.TEXT,
                limit: int = TEXT_LIMIT) -> QWidget:
    """Selectable label that truncates very long text behind an expand button."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lbl = QLabel(_clip(text, limit))
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setStyleSheet(f"color: {color};")
    if mono:
        lbl.setFont(_mono_font())
    lay.addWidget(lbl)
    if len(text) > limit:
        more = QPushButton(tr("show_all", n=len(text)))
        more.setFlat(True)
        more.setStyleSheet(f"color: {theme.ACCENT}; text-align: left; padding: 0;")
        more.setCursor(Qt.CursorShape.PointingHandCursor)

        def expand():
            lbl.setText(text)
            more.setVisible(False)
        more.clicked.connect(expand)
        lay.addWidget(more)
    return w


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n" + tr("more_chars", n=len(text) - limit)


def _mono_font():
    from PyQt6.QtGui import QFont
    f = QFont(MONO)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(9)
    return f


def result_has_error(text: str) -> bool:
    head = text[:400].lower()
    return ("error" in head or "exit code: 1" in head
            or "traceback" in head or "denied" in head)


class MessageFrame(QFrame):
    edit_requested = pyqtSignal(object)      # Message
    delete_requested = pyqtSignal(object)    # Message
    check_toggled = pyqtSignal(int, bool)    # line, checked

    def __init__(self, msg: Message, checkable: bool = False,
                 checked: bool = False, read_only: bool = False):
        super().__init__()
        self.msg = msg
        self.read_only = read_only
        self.setProperty("msgframe", True)
        self.setStyleSheet(f"QFrame[msgframe='true'] {{ border-left: 3px solid "
                           f"{theme.ROLE_COLORS.get(msg.role, theme.DIM)}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.check: QCheckBox | None = None
        if checkable:
            self.check = QCheckBox()
            self.check.setChecked(checked)
            self.check.toggled.connect(
                lambda on: self.check_toggled.emit(msg.line, on))
            head.addWidget(self.check)
        badge = QLabel(role_label(msg.role))
        badge.setStyleSheet(theme.badge_style(msg.role))
        head.addWidget(badge)
        if msg.sidechain:
            sc = QLabel(tr("subagent_badge"))
            sc.setStyleSheet(theme.badge_style("system"))
            head.addWidget(sc)
        if msg.model:
            m = QLabel(msg.model.split("/")[-1])
            m.setStyleSheet(f"color: {theme.DIM}; font-size: 11px;")
            head.addWidget(m)
        head.addStretch(1)
        if msg.ts:
            t = QLabel(util.fmt_dt(msg.ts))
            t.setStyleSheet(f"color: {theme.DIM}; font-size: 11px;")
            head.addWidget(t)
        ln = QLabel(f"L{msg.line}")
        ln.setStyleSheet(f"color: #565b68; font-size: 11px;")
        head.addWidget(ln)
        lay.addLayout(head)

        for part in msg.parts:
            if part.kind == "text":
                body = _long_label(part.text or "(empty)",
                                   color=theme.TEXT if msg.role != "system" else theme.DIM)
                lay.addWidget(body)
            elif part.kind in ("thinking", "reasoning"):
                lay.addWidget(Collapsible(tr("c_thinking"), _long_label(
                    part.text or "(encrypted)", color=theme.REASONING),
                    start_open=False, color=theme.REASONING))
            elif part.kind == "tool_call":
                lay.addWidget(Collapsible(tr("c_tool", name=part.name), _long_label(
                    part.text or "{}", mono=True, color=theme.TOOL), start_open=False))
            elif part.kind == "tool_result":
                head2 = QHBoxLayout()
                ok = not result_has_error(part.text)
                dot = QLabel(tr("res_ok") if ok else tr("res_err"))
                dot.setStyleSheet(f"color: {theme.OK if ok else '#e06c75'};"
                                  f" font-size: 11px;")
                head2.addWidget(dot)
                head2.addStretch(1)
                w = QWidget()
                wl = QVBoxLayout(w)
                wl.setContentsMargins(0, 0, 0, 0)
                wl.addLayout(head2)
                wl.addWidget(_long_label(part.text or "", mono=True,
                                         color="#c8cad2"))
                lay.addWidget(Collapsible(tr("c_result"), w, start_open=False))

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setToolTip(util.one_line(msg.plain_text(300), 400) or msg.role)

    def mouseDoubleClickEvent(self, ev) -> None:
        if not self.read_only and ev.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit(self.msg)
        super().mouseDoubleClickEvent(ev)

    def set_checked(self, on: bool) -> None:
        if self.check:
            self.check.blockSignals(True)
            self.check.setChecked(on)
            self.check.blockSignals(False)

    def _menu(self, pos) -> None:
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QApplication, QMenu
        menu = QMenu(self)
        if not self.read_only:
            a = QAction(tr("ctx_edit"), self)
            a.triggered.connect(lambda: self.edit_requested.emit(self.msg))
            menu.addAction(a)
        a2 = QAction(tr("ctx_copy"), self)
        a2.triggered.connect(
            lambda: QApplication.clipboard().setText(self.msg.plain_text(200000)))
        menu.addAction(a2)
        if not self.read_only:
            a3 = QAction(tr("ctx_delmsg"), self)
            a3.triggered.connect(lambda: self.delete_requested.emit(self.msg))
            menu.addAction(a3)
        menu.exec(self.mapToGlobal(pos))


class ChatView(QWidget):
    edit_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    delete_batch_requested = pyqtSignal(list)   # [Message]
    status_message = pyqtSignal(str)
    jumped = pyqtSignal()
    reverse_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.session: LoadedSession | None = None
        self._visible: list[Message] = []
        self._rendered = 0
        self._frames: dict[int, MessageFrame] = {}
        self._jump_line: int | None = None
        self._tr_closures: list = []
        self._batch = False
        self._checked: set[int] = set()
        self._reverse = False
        self.read_only = False
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        self.filter_combo = QComboBox()
        self._fill_filter_combo()
        self.filter_combo.currentTextChanged.connect(self._refilter)
        bar.addWidget(self.filter_combo)
        self.sidechain_check = QCheckBox(tr("cf_subagents"))
        self.sidechain_check.setChecked(True)
        self.sidechain_check.toggled.connect(self._refilter)
        self._tr_closures.append(
            lambda: self.sidechain_check.setText(tr("cf_subagents")))
        bar.addWidget(self.sidechain_check)
        self.reverse_check = QCheckBox(tr("ord_rev"))
        self.reverse_check.setToolTip(tr("ord_rev_tip"))
        self.reverse_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reverse_check.toggled.connect(self._on_reverse_toggled)
        self._tr_closures.append(lambda: (
            self.reverse_check.setText(tr("ord_rev")),
            self.reverse_check.setToolTip(tr("ord_rev_tip"))))
        bar.addWidget(self.reverse_check)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("in_session_ph"))
        self._tr_closures.append(
            lambda: self.search.setPlaceholderText(tr("in_session_ph")))
        self.search.textChanged.connect(self._refilter)
        bar.addWidget(self.search, 1)
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(f"color: {theme.DIM}; font-size: 11px;")
        bar.addWidget(self.count_lbl)
        self.batch_btn = QPushButton(tr("batch_sel"))
        self.batch_btn.setCheckable(True)
        self.batch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.batch_btn.toggled.connect(self._set_batch)
        self._tr_closures.append(lambda: self.batch_btn.setText(tr("batch_sel")))
        bar.addWidget(self.batch_btn)
        lay.addLayout(bar)

        # batch-mode action bar (visible while batch selection is on)
        self.batch_bar = QWidget()
        bl = QHBoxLayout(self.batch_bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        self.sel_lbl = QLabel("")
        self.sel_lbl.setStyleSheet(f"color: {theme.ACCENT}; font-size: 11px;")
        bl.addWidget(self.sel_lbl)
        small = f"QPushButton {{ color: {theme.TEXT}; font-size: 12px;" \
                f" padding: 2px 8px; }}"
        self.all_btn = QPushButton(tr("bmsg_all"))
        self.all_btn.setStyleSheet(small)
        self.all_btn.clicked.connect(self._select_visible)
        self.clear_btn = QPushButton(tr("bmsg_clear"))
        self.clear_btn.setStyleSheet(small)
        self.clear_btn.clicked.connect(self._clear_selection)
        self.copy_btn = QPushButton(tr("bmsg_copy"))
        self.copy_btn.setStyleSheet(small)
        self.copy_btn.clicked.connect(self._copy_selection)
        self.export_btn = QPushButton(tr("bmsg_export"))
        self.export_btn.setStyleSheet(small)
        self.export_btn.clicked.connect(self._export_selection)
        self.del_btn = QPushButton(tr("bmsg_del"))
        self.del_btn.setStyleSheet(
            f"QPushButton {{ color: #e06c75; font-size: 12px; padding: 2px 8px; }}")
        self.del_btn.clicked.connect(self._delete_selection)
        for b in (self.all_btn, self.clear_btn, self.copy_btn,
                  self.export_btn, self.del_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            bl.addWidget(b)
        bl.addStretch(1)
        self._tr_closures += [
            lambda: self.all_btn.setText(tr("bmsg_all")),
            lambda: self.clear_btn.setText(tr("bmsg_clear")),
            lambda: self.copy_btn.setText(tr("bmsg_copy")),
            lambda: self.export_btn.setText(tr("bmsg_export")),
            lambda: self.del_btn.setText(tr("bmsg_del")),
            self._update_sel_lbl,
        ]
        self.batch_bar.setVisible(False)
        lay.addWidget(self.batch_bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(6, 6, 6, 6)
        self.vbox.setSpacing(6)
        self.vbox.addStretch(1)
        self.scroll.setWidget(self.container)
        lay.addWidget(self.scroll, 1)

    def _fill_filter_combo(self) -> None:
        cur = self.filter_combo.currentData() if hasattr(self, "filter_combo") else "all"
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        for data, key in FILTER_MODES:
            self.filter_combo.addItem(tr(key), data)
        ix = self.filter_combo.findData(cur)
        self.filter_combo.setCurrentIndex(max(0, ix))
        self.filter_combo.blockSignals(False)

    # -- session ---------------------------------------------------------
    def set_session(self, session: LoadedSession | None) -> None:
        self.session = session
        self.read_only = bool(session and not session.meta.editable)
        if self.read_only and self._batch:
            self._set_batch(False)
            self.batch_btn.setChecked(False)
        self.batch_btn.setEnabled(not self.read_only)
        self._checked.clear()
        self._update_sel_lbl()
        self._refilter()

    def set_reverse(self, on: bool) -> None:
        """Newest-first reading order (used on toggle + at startup restore)."""
        self._reverse = on
        self.reverse_check.blockSignals(True)
        self.reverse_check.setChecked(on)
        self.reverse_check.blockSignals(False)
        self._refilter()

    def _on_reverse_toggled(self, on: bool) -> None:
        self._reverse = on
        self.reverse_changed.emit(on)
        self._refilter()

    def _refilter(self) -> None:
        if not self.session:
            return
        msgs = self.session.messages
        mode = self.filter_combo.currentData() or "all"
        q = self.search.text().strip().lower()
        show_sc = self.sidechain_check.isChecked()
        out = []
        for m in msgs:
            if not show_sc and m.sidechain:
                continue
            if mode == "user" and m.role != "user":
                continue
            if mode == "asst" and m.role != "assistant":
                continue
            if mode == "notool" and (m.is_tool_related or m.role == "reasoning"):
                continue
            if mode == "text" and (m.is_tool_related or m.role == "reasoning"
                                   or not m.text_parts):
                continue
            if q and q not in m.plain_text(100000).lower():
                continue
            out.append(m)
        if self._reverse:
            out.reverse()  # newest first; "load more" pages into the past
        self._visible = out
        self._rendered = 0
        self.count_lbl.setText(f"{len(out)} / {len(msgs)}")
        self._clear_frames()
        self._render_more()

    def _clear_frames(self) -> None:
        while self.vbox.count() > 1:
            item = self.vbox.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._frames.clear()

    def _render_more(self) -> None:
        if not self._visible:
            return
        chunk = self._visible[self._rendered: self._rendered + PAGE]
        stretch = self.vbox.takeAt(self.vbox.count() - 1)
        for m in chunk:
            f = MessageFrame(m, checkable=self._batch,
                             checked=m.line in self._checked,
                             read_only=self.read_only)
            f.edit_requested.connect(self._relay_edit)
            f.delete_requested.connect(self._relay_delete)
            f.check_toggled.connect(self._on_check)
            self._frames[m.line] = f
            self.vbox.addWidget(f)
        self._rendered += len(chunk)
        if self._rendered < len(self._visible):
            more = QPushButton(tr("load_more", n=len(self._visible) - self._rendered))
            more.setObjectName("loadmore")
            more.setFlat(True)
            more.setStyleSheet(f"color: {theme.ACCENT}; padding: 6px;")
            more.setCursor(Qt.CursorShape.PointingHandCursor)
            more.clicked.connect(self._load_more_clicked)
            self.vbox.addWidget(more)
        if stretch is not None:
            self.vbox.addItem(stretch)
        else:
            self.vbox.addStretch(1)
        if self._jump_line is not None:
            line, self._jump_line = self._jump_line, None
            QTimer.singleShot(50, lambda: self.jump_to_line(line))

    def _load_more_clicked(self) -> None:
        # remove the button, render next chunk
        for i in range(self.vbox.count() - 1, -1, -1):
            w = self.vbox.itemAt(i).widget()
            if w and w.objectName() == "loadmore":
                self.vbox.takeAt(i)
                w.deleteLater()
                break
        sb = self.scroll.verticalScrollBar()
        anchor = sb.value()
        self._render_more()
        sb.setValue(anchor)

    def _relay_edit(self, msg) -> None:
        self.edit_requested.emit(msg)

    def _relay_delete(self, msg) -> None:
        self.delete_requested.emit(msg)

    # -- batch selection ----------------------------------------------------
    def _set_batch(self, on: bool) -> None:
        if on and self.read_only:
            self.batch_btn.setChecked(False)
            return
        self._batch = on
        if not on:
            self._checked.clear()
        self.batch_bar.setVisible(on)
        self._update_sel_lbl()
        self._refilter()  # re-render frames with/without checkboxes

    def _on_check(self, line: int, on: bool) -> None:
        if on:
            self._checked.add(line)
        else:
            self._checked.discard(line)
        self._update_sel_lbl()

    def _selected_msgs(self) -> list[Message]:
        if not self.session:
            return []
        return [m for m in self.session.messages if m.line in self._checked]

    def _update_sel_lbl(self) -> None:
        n = len(self._checked)
        self.sel_lbl.setText(tr("sel_n", n=n))
        for b in (self.copy_btn, self.export_btn):
            b.setEnabled(n > 0)
        self.del_btn.setEnabled(n > 0 and not self.read_only)
        self.all_btn.setEnabled(bool(self._visible))

    def _select_visible(self) -> None:
        self._checked |= {m.line for m in self._visible}
        self._sync_checks()
        self._update_sel_lbl()

    def _clear_selection(self) -> None:
        self._checked.clear()
        self._sync_checks()
        self._update_sel_lbl()

    def _sync_checks(self) -> None:
        for line, f in list(self._frames.items()):
            f.set_checked(line in self._checked)

    def _copy_selection(self) -> None:
        from PyQt6.QtWidgets import QApplication
        msgs = self._selected_msgs()
        if not msgs:
            return
        text = "\n\n---\n\n".join(
            f"[{role_label(m.role)}] {m.plain_text(100000)}" for m in msgs)
        QApplication.clipboard().setText(text)
        self.status_message.emit(tr("msg_copied", n=len(msgs)))

    def _export_selection(self) -> None:
        import re as _re
        from PyQt6.QtWidgets import QFileDialog
        msgs = self._selected_msgs()
        if not msgs or self.session is None:
            return
        safe = _re.sub(r'[<>:"/\\|?*\r\n\t]', "_",
                       self.session.meta.display_title).strip(" ._") or "session"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("bmsg_export"), f"{safe}-{len(msgs)}msgs.md",
            "Markdown (*.md)")
        if not path:
            return
        lines = [f"# {self.session.meta.display_title}", ""]
        for msg in msgs:
            ts = util.fmt_dt(msg.ts) if msg.ts else ""
            lines.append(f"## {role_label(msg.role)}" + (f" · {ts}" if ts else ""))
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
        self.status_message.emit(tr("msg_exported", n=len(msgs), path=path))

    def _delete_selection(self) -> None:
        if self.read_only:
            return
        msgs = self._selected_msgs()
        if msgs:
            self.delete_batch_requested.emit(msgs)

    # -- jump --------------------------------------------------------------
    def jump_to_line(self, line: int) -> None:
        if self.session is None:
            return
        by_line = {m.line: m for m in self.session.messages}
        target = by_line.get(line)
        if target is None:
            return
        # normalize filters so the target becomes visible; the triggered
        # _refilter re-queues the jump at the end of _render_more.
        self._jump_line = line
        changed = False
        if (self.filter_combo.currentData() or "all") != "all":
            ix = self.filter_combo.findData("all")
            self.filter_combo.setCurrentIndex(max(0, ix))
            changed = True
        if self.search.text():
            self.search.clear()
            changed = True
        if target.sidechain and not self.sidechain_check.isChecked():
            self.sidechain_check.setChecked(True)
            changed = True
        if changed:
            return
        while line not in self._frames and self._rendered < len(self._visible):
            self._render_more()
        f = self._frames.get(line)
        if f is None:
            self._jump_line = None
            return
        self._jump_line = None
        self.scroll.ensureWidgetVisible(f, 0, 80)
        f.setStyleSheet(f"QFrame[msgframe='true'] {{ border: 1px solid {theme.ACCENT};"
                        f" border-left: 3px solid {theme.ACCENT}; }}")

        def unflash(frame=f):
            try:
                frame.setStyleSheet(
                    f"QFrame[msgframe='true'] {{ border-left: 3px solid "
                    f"{theme.ROLE_COLORS.get(frame.msg.role, theme.DIM)}; }}")
            except RuntimeError:
                pass  # frame deleted during a refilter
        QTimer.singleShot(1600, unflash)
        self.jumped.emit()

    def queue_jump(self, line: int) -> None:
        self._jump_line = line
        self.jump_to_line(line)

    # -- language ----------------------------------------------------------
    def retranslate(self) -> None:
        for fn in self._tr_closures:
            fn()
        self._fill_filter_combo()
        self._refilter()  # re-renders frames with translated labels
