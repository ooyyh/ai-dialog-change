"""Main window: session table + viewer + all actions + language switch."""
from __future__ import annotations

import json
import re
import traceback
import uuid as uuidlib
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QSettings, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy,
    QSplitter, QTableView, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from .. import claude_store, codex_store, launcher, local_store, transfer, transplant, util
from ..i18n import role_label, set_lang, tr
from ..jsonlio import FileChangedError
from ..models import LoadedSession, Message, SessionMeta
from ..util import MIN_DT
from ..scanner import ScanThread
from . import theme
from .chat_view import ChatView
from .dialogs import (
    AddMessageDialog, GlobalSearchDialog, MessageEditDialog, StatsDialog,
    TransplantDialog, export_markdown,
)
from .session_model import COLS, SessionFilterProxy, SessionTableModel

# sort key -> (i18n key, table column, value fn)
SORT_KEYS = [
    ("updated", "s_updated", 7, lambda m: m.updated or m.created or MIN_DT),
    ("created", "s_created", 8, lambda m: m.created or MIN_DT),
    ("title", "s_title", 2, lambda m: m.display_title.lower()),
    ("project", "s_project", 3, lambda m: m.project.lower()),
    ("size", "s_size", 5, lambda m: m.size),
    ("msgs", "s_msgs", 4, lambda m: m.n_messages),
]


def _confirm(parent, title: str, text: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    yes = box.addButton(tr("yes"), QMessageBox.ButtonRole.YesRole)
    no = box.addButton(tr("no"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\r\n\t]', "_", s).strip(" ._") or "session"


class LoadThread(QThread):
    loaded = pyqtSignal(object)

    def __init__(self, meta: SessionMeta, parent=None):
        super().__init__(parent)
        self.meta = meta

    def run(self) -> None:
        try:
            if self.meta.source == "claude":
                sess = claude_store.load_claude(self.meta)
            else:
                sess = codex_store.load_codex(self.meta)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            sess = None
        self.loaded.emit(sess)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("ai-dialog-manager", "adm")
        self.metas: list[SessionMeta] = []
        self.loaded: LoadedSession | None = None
        self.load_thread: LoadThread | None = None
        self.scan_thread: ScanThread | None = None
        self.search_dlg: GlobalSearchDialog | None = None
        self._watcher: QFileSystemWatcher | None = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(2500)
        self._refresh_timer.timeout.connect(self.refresh)
        self._tr_closures: list = []
        self._batch_acts: list[tuple[QAction, str]] = []
        self._build()
        self.refresh()

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        self.setWindowTitle(tr("app_title"))
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        for key, tip_key, slot in [
            ("tb_refresh", "tip_refresh", self.refresh),
            ("tb_search", "tip_search", self.global_search),
            ("tb_stats", "tip_stats", self.stats),
        ]:
            b = QPushButton(tr(key))
            b.setToolTip(tr(tip_key))
            b.clicked.connect(slot)
            self._tr_closures.append(
                lambda b=b, k=key, t=tip_key: (b.setText(tr(k)), b.setToolTip(tr(t))))
            tb.addWidget(b)
        tb.addSeparator()
        for key, tip_key, slot in [
            ("tb_resume", "tip_resume", self.resume_selected),
            ("tb_star", "tip_star", self.toggle_star),
            ("tb_rename", "tip_rename", self.rename_selected),
            ("tb_dup", "tip_dup", self.duplicate_selected),
            ("tb_upload", "tip_upload", self.upload_selected),
            ("tb_transplant", "tip_transplant", self.transplant_selected),
            ("tb_arch", "tip_arch", self.toggle_archive),
            ("tb_del", "tip_del", self.delete_selected),
        ]:
            b = QPushButton(tr(key))
            b.setToolTip(tr(tip_key))
            b.clicked.connect(slot)
            self._tr_closures.append(
                lambda b=b, k=key, t=tip_key: (b.setText(tr(k)), b.setToolTip(tr(t))))
            tb.addWidget(b)

        # batch dropdown
        self.batch_btn = QPushButton(tr("tb_batch"))
        self.batch_btn.setToolTip(tr("tip_batch"))
        batch_menu = QMenu(self.batch_btn)
        for key, slot in [
            ("b_star", lambda: self._batch_star(True)),
            ("b_unstar", lambda: self._batch_star(False)),
            ("b_archive", self._batch_archive),
            ("b_upload", self.upload_selected),
            ("b_transplant", self.transplant_selected),
            ("b_export", self._batch_export),
            ("b_delete", self._batch_delete),
        ]:
            # parent = menu (NOT self): Qt deletes the menu with its button;
            # actions parented elsewhere die in unpredictable order -> crash.
            a = QAction(tr(key), batch_menu)
            a.triggered.connect(slot)
            batch_menu.addAction(a)
            self._batch_acts.append((a, key))
        batch_menu.addSeparator()
        self.batch_btn.setMenu(batch_menu)  # click pops the menu natively
        self._tr_closures.append(lambda: (self.batch_btn.setText(tr("tb_batch")),
                                          self.batch_btn.setToolTip(tr("tip_batch")),
                                          [a.setText(tr(k)) for a, k in self._batch_acts]))
        tb.addWidget(self.batch_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        lang_label = QLabel("🌐")
        tb.addWidget(lang_label)
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("中文", "zh")
        self.lang_combo.addItem("English", "en")
        self.lang_combo.setToolTip(tr("lang_tip"))
        cur = self.settings.value("lang", "zh")
        ix = self.lang_combo.findData(cur)
        self.lang_combo.setCurrentIndex(max(0, ix))
        self.lang_combo.currentIndexChanged.connect(self._switch_lang)
        tb.addWidget(self.lang_combo)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ---- left: filters + table / grouped tree
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(6)
        fbar = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(tr("filter_ph"))
        self._tr_closures.append(
            lambda: self.filter_edit.setPlaceholderText(tr("filter_ph")))
        self.filter_edit.textChanged.connect(self._apply_filters)
        fbar.addWidget(self.filter_edit, 1)
        self.source_combo = QComboBox()
        self._fill_source_combo()
        self.source_combo.currentTextChanged.connect(self._apply_filters)
        fbar.addWidget(self.source_combo)
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(140)
        self.project_combo.currentTextChanged.connect(self._apply_filters)
        fbar.addWidget(self.project_combo)
        self.sort_combo = QComboBox()
        self._fill_sort_combo()
        self.sort_combo.currentIndexChanged.connect(self._apply_sort)
        fbar.addWidget(self.sort_combo)
        self.sort_dir_btn = QPushButton("↓")
        self.sort_dir_btn.setFixedWidth(30)
        self.sort_dir_btn.setToolTip(tr("sort_tip"))
        self.sort_dir_btn.clicked.connect(self._toggle_sort_dir)
        self._tr_closures.append(
            lambda: self.sort_dir_btn.setToolTip(tr("sort_tip")))
        fbar.addWidget(self.sort_dir_btn)
        self.star_btn = QPushButton(tr("starred_off"))
        self.star_btn.setCheckable(True)
        self.star_btn.toggled.connect(self._apply_filters)
        fbar.addWidget(self.star_btn)
        self.hide_arch_btn = QPushButton(tr("hide_archived"))
        self.hide_arch_btn.setCheckable(True)
        self.hide_arch_btn.setChecked(True)
        self.hide_arch_btn.toggled.connect(self._apply_filters)
        fbar.addWidget(self.hide_arch_btn)
        self.hide_sub_btn = QPushButton(tr("hide_subagents"))
        self.hide_sub_btn.setCheckable(True)
        self.hide_sub_btn.setChecked(True)
        self.hide_sub_btn.toggled.connect(self._apply_filters)
        fbar.addWidget(self.hide_sub_btn)
        self.group_btn = QPushButton(tr("group_by"))
        self.group_btn.setCheckable(True)
        self.group_btn.toggled.connect(self._set_group_mode)
        fbar.addWidget(self.group_btn)
        self._tr_closures += [
            lambda: self.hide_arch_btn.setText(tr("hide_archived")),
            lambda: self.hide_sub_btn.setText(tr("hide_subagents")),
            lambda: self.group_btn.setText(tr("group_by")),
        ]
        lv.addLayout(fbar)

        self.model = SessionTableModel()
        self.proxy = SessionFilterProxy()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(7, Qt.SortOrder.DescendingOrder)
        self.table.doubleClicked.connect(lambda _: self._load_selected())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(2, hh.ResizeMode.Stretch)
        lv.addWidget(self.table, 1)

        # grouped tree (project -> sessions), hidden until toggled on
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(self._tree_activated)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.setVisible(False)
        lv.addWidget(self.tree, 1)
        splitter.addWidget(left)

        # ---- right: viewer
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 6, 6, 6)
        rv.setSpacing(6)
        self.title_lbl = QLabel(tr("no_session"))
        self.title_lbl.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.title_lbl.setWordWrap(True)
        self._tr_closures.append(lambda: self.title_lbl.setText(tr("no_session"))
                                 if self.loaded is None else None)
        rv.addWidget(self.title_lbl)
        self.sub_lbl = QLabel(tr("dblclick_hint"))
        self.sub_lbl.setStyleSheet(f"color: {theme.DIM};")
        self.sub_lbl.setWordWrap(True)
        self._tr_closures.append(lambda: self.sub_lbl.setText(tr("dblclick_hint"))
                                 if self.loaded is None else None)
        rv.addWidget(self.sub_lbl)
        vbar = QHBoxLayout()
        self.addmsg_btn = QPushButton(tr("addmsg"))
        self.addmsg_btn.clicked.connect(self.add_message)
        self.reload_btn = QPushButton(tr("reload"))
        self.reload_btn.clicked.connect(lambda: self._load_selected(force=True))
        self.export_btn = QPushButton(tr("exportmd"))
        self.export_btn.clicked.connect(self.export_md)
        self._tr_closures += [
            lambda: self.addmsg_btn.setText(tr("addmsg")),
            lambda: self.reload_btn.setText(tr("reload")),
            lambda: self.export_btn.setText(tr("exportmd")),
        ]
        vbar.addWidget(self.addmsg_btn)
        vbar.addWidget(self.reload_btn)
        vbar.addWidget(self.export_btn)
        vbar.addStretch(1)
        rv.addLayout(vbar)
        self.chat = ChatView()
        self.chat.edit_requested.connect(self.edit_message)
        self.chat.delete_requested.connect(self.delete_message)
        self.chat.delete_batch_requested.connect(self.delete_messages)
        self.chat.status_message.connect(lambda s: self.status_lbl.setText(s))
        self.chat.reverse_changed.connect(self._persist_chat_reverse)
        if str(self.settings.value("chat_reverse", "0")).lower() in ("1", "true"):
            self.chat.set_reverse(True)
        rv.addWidget(self.chat, 1)
        splitter.addWidget(right)
        splitter.setSizes([640, 860])
        self.setCentralWidget(splitter)

        self.status_lbl = QLabel(tr("ready"))
        self.statusBar().addWidget(self.status_lbl, 1)

        # shortcuts promised by the placeholder texts
        from PyQt6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: (self.filter_edit.setFocus(), self.filter_edit.selectAll()))
        QShortcut(QKeySequence("Ctrl+Shift+F"), self).activated.connect(
            self.global_search)

    def _fill_source_combo(self) -> None:
        cur = self.source_combo.currentData() if hasattr(self, "source_combo") else "All"
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem(tr("all"), "All")
        self.source_combo.addItem("Claude", "Claude")
        self.source_combo.addItem("Codex", "Codex")
        ix = self.source_combo.findData(cur)
        self.source_combo.setCurrentIndex(max(0, ix))
        self.source_combo.blockSignals(False)

    def _fill_sort_combo(self) -> None:
        cur = self.sort_combo.currentData() if hasattr(self, "sort_combo") else "updated"
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        for data, key, _col, _fn in SORT_KEYS:
            self.sort_combo.addItem(tr(key), data)
        ix = self.sort_combo.findData(cur)
        self.sort_combo.setCurrentIndex(max(0, ix))
        self.sort_combo.blockSignals(False)

    def _persist_chat_reverse(self, on: bool) -> None:
        self.settings.setValue("chat_reverse", "1" if on else "0")

    # ------------------------------------------------------------ language
    def _switch_lang(self, index: int) -> None:
        lang = self.lang_combo.itemData(index)
        set_lang(lang)
        self.settings.setValue("lang", lang)
        if self.search_dlg:
            self.search_dlg.close()  # strings are fixed at construction
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("app_title"))
        for fn in self._tr_closures:
            fn()
        self._fill_source_combo()
        self._fill_sort_combo()
        self._fill_project_combo_first_item()
        self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0,
                                          len(COLS) - 1)
        self.chat.retranslate()
        self._apply_filters()
        self._apply_sort()
        self.status_lbl.setText(tr("ready"))
        if self.loaded is None:
            self.title_lbl.setText(tr("no_session"))
            self.sub_lbl.setText(tr("dblclick_hint"))

    def _fill_project_combo_first_item(self) -> None:
        if self.project_combo.count() > 0:
            self.project_combo.blockSignals(True)
            self.project_combo.setItemText(0, tr("all"))
            self.project_combo.blockSignals(False)

    # ------------------------------------------------------------ scanning
    def refresh(self) -> None:
        if self.scan_thread and self.scan_thread.isRunning():
            return
        self.status_lbl.setText(tr("scanning"))
        self.scan_thread = ScanThread(self)
        self.scan_thread.progress.connect(lambda s: self.status_lbl.setText(s))
        self.scan_thread.finished_scan.connect(self._scan_done)
        self.scan_thread.start()

    def _scan_done(self, metas: list) -> None:
        self.metas = metas
        keep_data = self.project_combo.currentData()
        projects = sorted({m.project for m in metas})
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        self.project_combo.addItem(tr("all"), "All")
        for p in projects:
            self.project_combo.addItem(p, p)
        if keep_data:
            ix = self.project_combo.findData(keep_data)
            self.project_combo.setCurrentIndex(max(0, ix))
        self.project_combo.blockSignals(False)
        self.model.set_rows(metas)
        n_claude = sum(1 for m in metas if m.source == "claude")
        self.status_lbl.setText(tr("status_sessions", n=len(metas), c=n_claude,
                                   x=len(metas) - n_claude))
        self._install_watcher()

    def _install_watcher(self) -> None:
        from .. import config as cfg
        dirs = set()
        # New session files land inside a project dir (claude) or a YYYY/MM/DD
        # day dir (codex). QFileSystemWatcher does NOT report changes in
        # subdirectories of a watched path, so watch every existing level.
        if cfg.CLAUDE_PROJECTS.is_dir():
            dirs.add(str(cfg.CLAUDE_PROJECTS))
            for d in cfg.CLAUDE_PROJECTS.iterdir():
                if d.is_dir():
                    dirs.add(str(d))
        if cfg.CODEX_SESSIONS.is_dir():
            dirs.add(str(cfg.CODEX_SESSIONS))
            for sub in cfg.CODEX_SESSIONS.iterdir():
                if sub.is_dir():
                    dirs.add(str(sub))
                    for sub2 in sub.iterdir():
                        if sub2.is_dir():
                            dirs.add(str(sub2))
        if self._watcher is not None:
            self._watcher.removePaths(self._watcher.directories())
        else:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.directoryChanged.connect(
                lambda _: self._refresh_timer.start())
        if dirs:
            self._watcher.addPaths(sorted(dirs))

    def _apply_filters(self) -> None:
        p = self.proxy
        p.text = self.filter_edit.text().strip()
        p.source_filter = self.source_combo.currentData() or "All"
        p.project_filter = self.project_combo.currentData() or "All"
        p.starred_only = self.star_btn.isChecked()
        self.star_btn.setText(tr("starred_on") if self.star_btn.isChecked()
                              else tr("starred_off"))
        p.hide_archived = self.hide_arch_btn.isChecked()
        p.hide_subagents = self.hide_sub_btn.isChecked()
        p.apply()
        if self.group_btn.isChecked():
            self._rebuild_tree()

    # ------------------------------------------------------------ sorting
    def _sort_spec(self):
        key = self.sort_combo.currentData() or "updated"
        for k, _i18n, col, fn in SORT_KEYS:
            if k == key:
                return k, col, fn
        return "updated", 7, SORT_KEYS[0][3]

    def _sort_desc(self) -> bool:
        return self.sort_dir_btn.text() == "↓"

    def _toggle_sort_dir(self) -> None:
        self.sort_dir_btn.setText("↑" if self._sort_desc() else "↓")
        self._apply_sort()

    def _apply_sort(self) -> None:
        if self.group_btn.isChecked():
            self._rebuild_tree()
            return
        _key, col, _fn = self._sort_spec()
        order = (Qt.SortOrder.DescendingOrder if self._sort_desc()
                 else Qt.SortOrder.AscendingOrder)
        self.table.sortByColumn(col, order)

    # ------------------------------------------------------------ grouping
    def _set_group_mode(self, on: bool) -> None:
        self.table.setVisible(not on)
        self.tree.setVisible(on)
        if on:
            self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        t = self.tree
        # remember selection/current across the rebuild (batch ops re-filter
        # after applying, and losing the selection would hit only 1 item next)
        prev_sel: set[tuple[str, str]] = set()
        cur_key = None
        if t.topLevelItemCount():
            for it in t.selectedItems():
                m = it.data(0, Qt.ItemDataRole.UserRole)
                if m:
                    prev_sel.add((m.source, m.id))
            cit = t.currentItem()
            if cit:
                m = cit.data(0, Qt.ItemDataRole.UserRole)
                if m:
                    cur_key = (m.source, m.id)
        t.blockSignals(True)
        t.clear()
        t.setHeaderLabels([tr("col_title"), tr("col_source"), tr("col_msgs"),
                           tr("col_size"), tr("col_updated")])
        key, _col, keyfn = self._sort_spec()
        desc = self._sort_desc()
        groups: dict[str, list[SessionMeta]] = {}
        for m in self.proxy.filtered_metas():
            groups.setdefault(m.project, []).append(m)

        def group_key(ms: list[SessionMeta]):
            vals = [keyfn(m) for m in ms]
            if key in ("size", "msgs"):
                return sum(vals)
            if key in ("updated", "created"):
                return max(vals)
            return min(vals)  # strings

        ordered = sorted(groups.items(), key=lambda kv: group_key(kv[1]),
                         reverse=desc)
        for proj, ms in ordered:
            ms.sort(key=keyfn, reverse=desc)
            latest = max((m.updated or m.created or MIN_DT) for m in ms)
            top = QTreeWidgetItem([
                proj, tr("g_sessions", n=len(ms)),
                f"{sum(m.n_messages for m in ms):,}",
                util.fmt_size(sum(m.size for m in ms)),
                util.fmt_dt(latest)])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setForeground(1, QColor(theme.DIM))
            t.addTopLevelItem(top)
            for m in ms:
                label = ("★ " if m.starred else "") + m.display_title
                if m.archived:
                    label += "  🗂"
                if m.subagent:
                    label += "  ⧉"
                it = QTreeWidgetItem([label, m.source_badge, str(m.n_messages),
                                      util.fmt_size(m.size),
                                      util.fmt_dt(m.updated or m.created)])
                it.setData(0, Qt.ItemDataRole.UserRole, m)
                it.setForeground(1, QColor("#5b8cff" if m.source == "claude"
                                           else "#4ec9a8"))
                if m.archived:
                    for c in range(5):
                        it.setForeground(c, QColor("#777d8c"))
                top.addChild(it)
        t.expandAll()
        # restore selection / current item
        for i in range(t.topLevelItemCount()):
            top = t.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                m = it.data(0, Qt.ItemDataRole.UserRole)
                if not m:
                    continue
                k = (m.source, m.id)
                if k in prev_sel:
                    it.setSelected(True)
                if k == cur_key:
                    t.setCurrentItem(it)
        t.blockSignals(False)

    def _tree_activated(self, item: QTreeWidgetItem, _col: int) -> None:
        m = item.data(0, Qt.ItemDataRole.UserRole)
        if m:
            self._load_selected(meta=m)

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return
        m = item.data(0, Qt.ItemDataRole.UserRole)
        if m:
            self._ctx_menu(m, self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ selection
    def _selected(self) -> SessionMeta | None:
        ix = self.table.currentIndex()
        if self.group_btn.isChecked():
            item = self.tree.currentItem()
            if item:
                return item.data(0, Qt.ItemDataRole.UserRole)
            return None
        if not ix.isValid():
            return None
        sr = self.proxy.mapToSource(ix)
        return self.model.meta_at(sr.row())

    def selected_metas(self) -> list[SessionMeta]:
        """All selected sessions (multi-select) in either view mode."""
        out: list[SessionMeta] = []
        seen: set[tuple[str, str]] = set()
        if self.group_btn.isChecked():
            for item in self.tree.selectedItems():
                m = item.data(0, Qt.ItemDataRole.UserRole)
                if m and (m.source, m.id) not in seen:
                    seen.add((m.source, m.id))
                    out.append(m)
        else:
            sm = self.table.selectionModel()
            if sm:
                for ix in sm.selectedRows(0):
                    m = self.model.meta_at(self.proxy.mapToSource(ix).row())
                    if m and (m.source, m.id) not in seen:
                        seen.add((m.source, m.id))
                        out.append(m)
        if not out:
            m = self._selected()
            if m:
                out = [m]
        return out

    def _table_menu(self, pos) -> None:
        m = self._selected()
        if not m:
            return
        self._ctx_menu(m, self.table.viewport().mapToGlobal(pos))

    def _ctx_menu(self, m: SessionMeta, gp) -> None:
        menu = QMenu(self)
        for key, slot in [
            ("ctx_open", lambda: self._load_selected(meta=m)),
            ("ctx_resume", lambda: launcher.resume(m)),
            ("ctx_star", lambda: self.toggle_star()),
            ("ctx_rename", lambda: self.rename_selected()),
            ("ctx_note", lambda: self.set_note()),
            ("ctx_upload", lambda: self._upload_metas([m])),
            ("ctx_transplant", lambda: self._transplant_metas([m])),
            ("ctx_reveal", lambda: self._reveal_file(m)),
            ("ctx_copyid", lambda: QApplication.clipboard().setText(m.id)),
            ("ctx_delete", lambda: self.delete_selected()),
        ]:
            a = QAction(tr(key), self)
            a.triggered.connect(slot)
            menu.addAction(a)
        menu.exec(gp)

    def _reveal_file(self, m: SessionMeta) -> None:
        import subprocess
        subprocess.Popen(["explorer", f"/select,{m.path}"],
                         cwd=str(Path(m.path).parent))

    # ------------------------------------------------------------ loading
    def _load_selected(self, force: bool = False, meta: SessionMeta | None = None,
                       line: int | None = None,
                       jump_idx: int | None = None) -> None:
        m = meta or self._selected()
        if m is None:
            return
        if self.loaded and not force and \
                self.loaded.meta.source == m.source and self.loaded.meta.id == m.id:
            if line is not None:
                self.chat.queue_jump(line)
            return
        if self.load_thread and self.load_thread.isRunning():
            return
        self.title_lbl.setText(m.display_title)
        self.sub_lbl.setText(tr("loading"))
        self.chat.set_session(None)
        self._pending_jump = line
        self._pending_jump_idx = jump_idx
        self.load_thread = LoadThread(m, self)
        self.load_thread.loaded.connect(self._loaded)
        self.load_thread.start()

    _pending_jump: int | None = None
    _pending_jump_idx: int | None = None

    def _loaded(self, sess) -> None:
        if sess is None:
            self.sub_lbl.setText(tr("load_failed"))
            return
        self.loaded = sess
        m = sess.meta
        rec = local_store.get(m.source, m.id)
        m.starred = rec.get("starred", False)
        bits = tr("sub_info",
                  source=m.source_badge, cwd=m.cwd or m.project,
                  lines=sess.total_lines, msgs=len(sess.messages),
                  size=util.fmt_size(m.size),
                  created=util.fmt_dt(m.created) or "–",
                  updated=util.fmt_dt(m.updated) or "–")
        if m.running:
            bits += " · " + tr("running_badge")
        if m.archived:
            bits += " · " + tr("archived_badge")
        self.sub_lbl.setText(bits)
        self.chat.set_session(sess)
        if self._pending_jump is not None:
            line, self._pending_jump = self._pending_jump, None
            self.chat.queue_jump(line)
        elif self._pending_jump_idx is not None:
            # index into the (reloaded) message list — used after deletions,
            # where old line numbers no longer match the rewritten file
            idx, self._pending_jump_idx = self._pending_jump_idx, None
            if sess.messages:
                self.chat.queue_jump(
                    sess.messages[min(idx, len(sess.messages) - 1)].line)

    # ------------------------------------------------------------ actions
    def resume_selected(self) -> None:
        m = self._selected() or (self.loaded.meta if self.loaded else None)
        if m:
            launcher.resume(m)

    def toggle_star(self) -> None:
        m = self._selected() or (self.loaded.meta if self.loaded else None)
        if not m:
            return
        now = local_store.toggle_star(m.source, m.id)
        m.starred = now
        self.model.update_row(m)
        self._apply_filters()

    def set_note(self) -> None:
        m = self._selected() or (self.loaded.meta if self.loaded else None)
        if not m:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, tr("note_title"), tr("note_label"), m.note)
        if ok:
            local_store.update(m.source, m.id, note=text)
            m.note = text

    def rename_selected(self) -> None:
        m = self._selected() or (self.loaded.meta if self.loaded else None)
        if not m:
            return
        text, ok = QInputDialog.getText(
            self, tr("rename_title"), tr("rename_label"), text=m.display_title)
        if not ok or not text.strip():
            return
        title = text.strip()
        try:
            if m.source == "claude":
                from ..jsonlio import SessionEditor
                ed = SessionEditor(m.path)
                ed.note_snapshot("claude", m.id)
                claude_store.append_title(ed, m.id, title)
                ed.save()
            else:
                codex_store.rename_thread(m.id, title)
        except FileChangedError:
            QMessageBox.warning(self, tr("changed_disk_title"),
                                tr("changed_disk_short"))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("rename_failed"), str(e))
            return
        local_store.update(m.source, m.id, custom_title="")
        m.title = title
        m.custom_title = ""
        self.model.update_row(m)
        self.title_lbl.setText(m.display_title)
        if self.loaded and self.loaded.meta.id == m.id:
            self.loaded.meta.title = title

    def toggle_archive(self) -> None:
        m = self._selected() or (self.loaded.meta if self.loaded else None)
        if not m:
            return
        if m.source != "codex":
            QMessageBox.information(self, tr("arch_claude_title"),
                                    tr("arch_claude_body"))
            return
        try:
            codex_store.set_archived(m.id, not m.archived)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("del_failed"), str(e))
            return
        m.archived = not m.archived
        self.model.update_row(m)
        self._apply_filters()

    def duplicate_selected(self) -> None:
        m = self._selected() or (self.loaded.meta if self.loaded else None)
        if not m:
            return
        try:
            if m.source == "claude":
                new_id = _duplicate_claude(m)
            else:
                new_id = codex_store.duplicate_session(m)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("del_failed"), str(e))
            return
        if new_id:
            QMessageBox.information(
                self, tr("dup_done_title"), tr("dup_done_body", id=new_id))
            self.refresh()

    # ------------------------------------------------------- consciousness upload
    def upload_selected(self) -> None:
        self._upload_metas(self.selected_metas())

    def _upload_metas(self, metas: list[SessionMeta]) -> None:
        """Export selected sessions as portable, resumable bundles."""
        if not metas:
            QMessageBox.information(self, tr("app_title"), tr("b_none"))
            return
        start = str(self.settings.value(
            "upload_dir", str(Path.home() / "Downloads")))
        d = QFileDialog.getExistingDirectory(self, tr("upload_pick"), start)
        if not d:
            return
        self.settings.setValue("upload_dir", d)
        ok = 0
        last_bundle: Path | None = None
        errs: list[str] = []
        for i, m in enumerate(metas):
            self.status_lbl.setText(f"{i + 1}/{len(metas)} ⤴ {m.display_title[:40]}")
            QApplication.processEvents()
            try:
                last_bundle = transfer.export_session(m, Path(d))
                ok += 1
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                errs.append(f"{m.display_title[:50]}: {e}")
        if len(metas) == 1 and ok == 1 and last_bundle is not None:
            self.status_lbl.setText(tr("upload_done_one", path=str(last_bundle)))
            QMessageBox.information(
                self, tr("upload_done_title"),
                tr("upload_done_body", path=str(last_bundle)))
        else:
            self.status_lbl.setText(tr("upload_done", n=ok, m=len(metas), dir=d))
        if errs:
            more = "\n…" if len(errs) > 5 else ""
            QMessageBox.warning(self, tr("upload_failed"),
                                "\n".join(errs[:5]) + more)

    # ------------------------------------------------------- consciousness transplant
    def transplant_selected(self) -> None:
        """Adapt the selected session(s) into another AI tool's store."""
        self._transplant_metas(self.selected_metas())

    def _transplant_metas(self, metas: list[SessionMeta]) -> None:
        if not metas:
            QMessageBox.information(self, tr("app_title"), tr("b_none"))
            return
        first = metas[0]
        dlg = TransplantDialog(
            first, self,
            last_target=str(self.settings.value("transplant_target", "codex")),
            last_cwd=str(self.settings.value("transplant_cwd", "")))
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        target, cwd, title, opt = dlg.values()
        self.settings.setValue("transplant_target", target)
        self.settings.setValue("transplant_cwd", cwd)
        ok = 0
        results: list[transplant.TransplantResult] = []
        errs: list[str] = []
        for i, m in enumerate(metas):
            self.status_lbl.setText(
                f"{i + 1}/{len(metas)} ⤴ {m.display_title[:40]}")
            QApplication.processEvents()
            try:
                res = transplant.transplant(
                    m, target, cwd,
                    title=(title if len(metas) == 1 else m.display_title),
                    options=opt, out_dir=(cwd if target == "json" else None))
                results.append(res)
                ok += 1
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                errs.append(f"{m.display_title[:50]}: {e}")
        if len(metas) == 1 and ok == 1:
            res = results[0]
            body = tr("tp_done_body", target=tr("tp_" + res.target),
                      path=res.path, id=res.session_id,
                      resume=res.resume_hint())
            if res.warnings:
                body += "\n\n⚠ " + "\n⚠ ".join(res.warnings)
            self.status_lbl.setText("✓ " + res.path)
            QMessageBox.information(self, tr("tp_done_title"), body)
        else:
            self.status_lbl.setText(
                tr("tp_done_multi", n=ok, m=len(metas),
                   target=tr("tp_" + target), cwd=cwd))
        if errs:
            more = "\n…" if len(errs) > 5 else ""
            QMessageBox.warning(self, tr("tp_failed"),
                                "\n".join(errs[:5]) + more)
        if target in ("claude", "codex"):
            self.refresh()

    # ------------------------------------------------------------ batch
    def _batch_star(self, on: bool) -> None:
        metas = self.selected_metas()
        if not metas:
            QMessageBox.information(self, tr("app_title"), tr("b_none"))
            return
        for m in metas:
            local_store.update(m.source, m.id, starred=on)
            m.starred = on
            self.model.update_row(m)
        self.status_lbl.setText(tr("b_done", n=len(metas)))
        self._apply_filters()

    def _batch_archive(self) -> None:
        metas = self.selected_metas()
        if not metas:
            QMessageBox.information(self, tr("app_title"), tr("b_none"))
            return
        cx = [m for m in metas if m.source == "codex"]
        skipped = len(metas) - len(cx)
        if not cx:
            QMessageBox.information(self, tr("arch_claude_title"),
                                    tr("arch_claude_body"))
            return
        target = not all(m.archived for m in cx)
        err = 0
        for m in cx:
            try:
                codex_store.set_archived(m.id, target)
                m.archived = target
                self.model.update_row(m)
            except Exception:  # noqa: BLE001
                err += 1
        msg = tr("b_done", n=len(cx) - err)
        if skipped:
            msg += " · " + tr("b_arch_skipped", n=skipped)
        self.status_lbl.setText(msg)
        self._apply_filters()

    def _batch_export(self) -> None:
        metas = self.selected_metas()
        if not metas:
            QMessageBox.information(self, tr("app_title"), tr("b_none"))
            return
        d = QFileDialog.getExistingDirectory(self, tr("b_exp_pick"),
                                             str(Path.home() / "Downloads"))
        if not d:
            return
        ok = 0
        for i, m in enumerate(metas):
            self.status_lbl.setText(f"{i + 1}/{len(metas)} {m.display_title[:40]}")
            QApplication.processEvents()
            try:
                if m.source == "claude":
                    sess = claude_store.load_claude(m)
                else:
                    sess = codex_store.load_codex(m)
                fname = f"{_safe_name(m.display_title)[:60]}-{m.id[:8]}.md"
                export_markdown(sess, str(Path(d) / fname))
                ok += 1
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.status_lbl.setText(tr("b_exp_done", n=ok, m=len(metas), dir=d))

    def _batch_delete(self) -> None:
        self.delete_selected()

    # delete works on the whole selection (single selection = classic message)
    def delete_selected(self) -> None:
        metas = self.selected_metas()
        if not metas:
            return
        total = sum(m.size for m in metas)
        if len(metas) == 1:
            m = metas[0]
            body = tr("del_sess_body", title=m.display_title,
                      size=util.fmt_size(m.size), id=m.id)
        else:
            body = tr("b_del_body", n=len(metas), size=util.fmt_size(total))
        if not _confirm(self, tr("del_sess_title"), body):
            return
        deleted: set[tuple[str, str]] = set()
        failed = 0
        for m in metas:
            if self._remove_session(m):
                deleted.add((m.source, m.id))
            else:
                failed += 1
        if failed:
            QMessageBox.warning(self, tr("del_failed"),
                                tr("b_done", n=failed))
        if self.loaded and (self.loaded.meta.source, self.loaded.meta.id) in deleted:
            self.loaded = None
            self.chat.set_session(None)
            self.title_lbl.setText(tr("no_session"))
            self.sub_lbl.setText(tr("dblclick_hint"))
        self.refresh()

    def _remove_session(self, m: SessionMeta) -> bool:
        """Backup + delete one session file (and its codex sqlite rows)."""
        from ..jsonlio import make_backup
        from .. import config as cfg
        try:
            make_backup(m.path, m.source, m.id)
            Path(m.path).unlink(missing_ok=True)
            if m.source == "codex":
                try:
                    codex_store.invalidate_history(m.id)
                    with codex_store._connect(cfg.CODEX_STATE_DB,
                                              readonly=False) as con:
                        con.execute("DELETE FROM threads WHERE id=?", (m.id,))
                except Exception:  # noqa: BLE001 — sqlite is best-effort here
                    pass
        except OSError:
            return False
        return True

    # ------------------------------------------------------------ editing
    def edit_message(self, msg: Message) -> None:
        if not self.loaded:
            return
        m = self.loaded.meta
        if m.running and not _confirm(self, tr("run_title"), tr("run_body")):
            return
        dlg = MessageEditDialog(msg, self)
        if dlg.exec() != MessageEditDialog.DialogCode.Accepted:
            return
        try:
            if m.source == "claude":
                ed = claude_store.make_editor(self.loaded)
                if dlg.use_raw:
                    claude_store.apply_raw_edit(ed, msg, dlg.raw_json())
                else:
                    claude_store.apply_text_edits(ed, msg, dlg.new_texts())
                ed.save()
            else:
                ed = codex_store.make_editor(self.loaded)
                if dlg.use_raw:
                    codex_store.apply_raw_edit(ed, msg, dlg.raw_json())
                else:
                    codex_store.apply_text_edits(ed, msg, dlg.new_texts())
                ed.save()
                codex_store.invalidate_history(m.id)
        except FileChangedError:
            QMessageBox.warning(self, tr("changed_disk_title"),
                                tr("changed_disk_edit"))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("edit_failed"), f"{e}")
            return
        self.status_lbl.setText(tr("saved_backup"))
        self._load_selected(force=True, meta=m, line=msg.line)

    def delete_message(self, msg: Message) -> None:
        self._delete_messages([msg])

    def delete_messages(self, msgs) -> None:
        self._delete_messages(list(msgs))

    def _delete_messages(self, msgs: list[Message]) -> None:
        if not self.loaded or not msgs:
            return
        m = self.loaded.meta
        store = claude_store if m.source == "claude" else codex_store
        linked: set[int] = set()
        for msg in msgs:
            linked |= store.linked_lines(self.loaded, msg)
        if not linked:
            return
        removed = [x for x in self.loaded.messages if x.line in linked]
        # scroll anchor: first survivor after the removed block, else the last
        # survivor before it — the reload jumps back here (index into the
        # surviving message list, since line numbers shift after deletion)
        survivors = [x for x in self.loaded.messages if x.line not in linked]
        maxln, minln = max(linked), min(linked)
        anchor = next((x for x in survivors if x.line > maxln), None)
        if anchor is None:
            before = [x for x in survivors if x.line < minln]
            anchor = before[-1] if before else None
        anchor_idx = survivors.index(anchor) if anchor is not None else None
        if len(msgs) == 1:
            extra = tr("delmsg_linked", n=len(linked) - 1) if len(linked) > 1 else ""
            body = tr("delmsg_body", role=role_label(msgs[0].role),
                      line=msgs[0].line) + extra
        else:
            body = tr("delmsgs_body", n=len(removed),
                      extra=len(removed) - len(msgs), lines=len(linked))
        if not _confirm(self, tr("delmsg_title"), body):
            return
        try:
            ed = store.make_editor(self.loaded)
            ed.delete_lines(linked)
            if m.source == "claude":
                # re-link parentUuid chain + last-prompt.leafUuid, otherwise
                # the CLI can't rebuild the tree and won't render the session
                claude_store.repair_after_delete(ed, linked)
            ed.save()
            if m.source == "codex":
                codex_store.invalidate_history(m.id)
        except FileChangedError:
            QMessageBox.warning(self, tr("changed_disk_title"),
                                tr("changed_disk_short"))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("del_failed"), str(e))
            return
        if len(msgs) == 1:
            self.status_lbl.setText(tr("delmsg_done"))
        else:
            self.status_lbl.setText(tr("delmsgs_done", n=len(removed)))
        self._load_selected(force=True, meta=m, jump_idx=anchor_idx)

    def add_message(self) -> None:
        if not self.loaded:
            return
        m = self.loaded.meta
        dlg = AddMessageDialog(self)
        if m.running:
            dlg.warn.setText(tr("add_warn"))
        if dlg.exec() != AddMessageDialog.DialogCode.Accepted or not dlg.text():
            return
        try:
            ed = claude_store.make_editor(self.loaded) if m.source == "claude" \
                else codex_store.make_editor(self.loaded)
            if m.source == "claude":
                claude_store.append_user_message(ed, m.id, m.cwd, dlg.text())
            else:
                codex_store.append_user_message(ed, dlg.text())
            ed.save()
            if m.source == "codex":
                codex_store.invalidate_history(m.id)
        except FileChangedError:
            QMessageBox.warning(self, tr("changed_disk_title"),
                                tr("changed_disk_short"))
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, tr("append_failed"), str(e))
            return
        self._load_selected(force=True, meta=m)

    # ------------------------------------------------------------ misc
    def global_search(self) -> None:
        if not self.metas:
            return
        if self.search_dlg is None:
            self.search_dlg = GlobalSearchDialog(self.metas, self)
            self.search_dlg.open_hit.connect(
                lambda meta, line: self._load_selected(meta=meta, line=line))
        else:
            self.search_dlg.metas = self.metas
        self.search_dlg.show()
        self.search_dlg.raise_()
        self.search_dlg.activateWindow()
        self.search_dlg.query.setFocus()

    def stats(self) -> None:
        if self.metas:
            StatsDialog(self.metas, self).exec()

    def export_md(self) -> None:
        if not self.loaded:
            return
        default = str(Path.home() / "Downloads" /
                      (self.loaded.meta.display_title[:50].replace(" ", "_")
                       or "session") + ".md")
        path, _ = QFileDialog.getSaveFileName(self, tr("export_title"), default,
                                              "Markdown (*.md)")
        if path:
            try:
                export_markdown(self.loaded, path)
                self.status_lbl.setText(tr("export_done", path=path))
            except OSError as e:
                QMessageBox.critical(self, tr("del_failed"), str(e))

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_F5:
            self.refresh()
        elif e.key() == Qt.Key.Key_Delete:
            self.delete_selected()
        elif e.key() == Qt.Key.Key_Return and self.table.hasFocus():
            self._load_selected()
        else:
            super().keyPressEvent(e)


def _duplicate_claude(m: SessionMeta) -> str | None:
    new_id = str(uuidlib.uuid4())
    src = Path(m.path)
    out_lines = []
    for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            out_lines.append(line)
            continue
        if "sessionId" in o:
            o["sessionId"] = new_id
        if o.get("type") in ("ai-title", "last-prompt"):
            continue
        out_lines.append(util.dumps_claude(o))
    (src.parent / f"{new_id}.jsonl").write_text(
        "\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")
    return new_id
