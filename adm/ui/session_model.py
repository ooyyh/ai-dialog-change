"""Table model + filter proxy for the unified session list."""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QColor

from .. import util
from ..i18n import tr
from ..util import MIN_DT
from ..models import SessionMeta

COLS = ["★", "col_source", "col_title", "col_project", "col_msgs", "col_size",
        "col_tokens", "col_updated", "col_created", "col_status"]
SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class SessionTableModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[SessionMeta] = []

    def set_rows(self, rows: list[SessionMeta]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def update_row(self, meta: SessionMeta) -> None:
        for i, m in enumerate(self.rows):
            if m.source == meta.source and m.id == meta.id:
                self.rows[i] = meta
                ix = self.index(i, 0)
                self.dataChanged.emit(ix, self.index(i, len(COLS) - 1))
                return
        self.beginInsertRows(self.index(), len(self.rows), len(self.rows))
        self.rows.append(meta)
        self.endInsertRows()

    def meta_at(self, row: int) -> SessionMeta | None:
        return self.rows[row] if 0 <= row < len(self.rows) else None

    # Qt plumbing -------------------------------------------------------
    def rowCount(self, parent=None) -> int:
        return 0 if parent and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None) -> int:
        return len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            key = COLS[s]
            return key if key.startswith("★") else tr(key)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        m = self.rows[index.row()]
        c = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            if c == 0:
                if role == SORT_ROLE:
                    return int(m.starred)
                return "★" if m.starred else "☆"
            if c == 1:
                return m.source_badge
            if c == 2:
                if role == SORT_ROLE:
                    return m.display_title.lower()
                return m.display_title
            if c == 3:
                if role == SORT_ROLE:
                    return m.project.lower()
                return m.project
            if c == 4:
                return m.n_messages
            if c == 5:
                return m.size
            if c == 6:
                return m.tokens if m.tokens is not None else -1
            if c == 7:
                return m.updated or m.created or MIN_DT
            if c == 8:
                return m.created or MIN_DT
            if c == 9:
                if role == SORT_ROLE:
                    return (4 if m.running else 0) + (2 if m.archived else 0) + \
                        (1 if m.subagent else 0)
                bits = []
                if m.running:
                    bits.append("running")
                if m.archived:
                    bits.append("archived")
                if m.subagent:
                    bits.append("sub")
                return " ".join(bits)
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = (f"{m.display_title}\n{m.cwd or m.path}\n"
                   f"{m.source} · {m.n_user}u/{m.n_assistant}a · {util.fmt_size(m.size)}\n"
                   f"id: {m.id}")
            if m.note:
                tip += f"\nnote: {m.note}"
            return tip
        if role == Qt.ItemDataRole.ForegroundRole:
            if c == 0 and m.starred:
                return QColor("#e9c46a")
            if c == 1:
                return QColor("#5b8cff" if m.source == "claude" else "#4ec9a8")
            if c == 9:
                if m.running:
                    return QColor("#5ec269")
                if m.archived:
                    return QColor("#9aa1b2")
            if m.archived:
                return QColor("#777d8c")
        if role == Qt.ItemDataRole.TextAlignmentRole and c in (4, 5, 6):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        # formatted display for the value columns
        if role == Qt.ItemDataRole.DisplayRole and c in (5, 6, 7, 8):
            v = self.data(index, SORT_ROLE)
            if c == 5:
                return util.fmt_size(v)
            if c == 6:
                return util.fmt_tokens(v) if v and v > 0 else ""
            return util.fmt_dt(v)
        return None


class SessionFilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.setSortRole(SORT_ROLE)
        self.text = ""
        self.source_filter = "All"
        self.project_filter = "All"
        self.starred_only = False
        self.hide_archived = False
        self.hide_subagents = True

    def apply(self) -> None:
        self.invalidateFilter()

    def filtered_metas(self) -> list:
        """The metas passing the current filters, in source order (for the
        grouped tree view, which sorts for itself)."""
        out = []
        model = self.sourceModel()
        for r in range(model.rowCount()):
            if self.filterAcceptsRow(r, QModelIndex()):
                out.append(model.rows[r])
        return out

    def filterAcceptsRow(self, row: int, parent) -> bool:
        m: SessionMeta = self.sourceModel().rows[row]
        if self.starred_only and not m.starred:
            return False
        if self.hide_archived and m.archived:
            return False
        if self.hide_subagents and m.subagent:
            return False
        if self.source_filter != "All" and m.source_badge != self.source_filter:
            return False
        if self.project_filter != "All" and m.project != self.project_filter:
            return False
        if self.text:
            t = self.text.lower()
            hay = " ".join([m.display_title, m.preview, m.project, m.id]).lower()
            if t not in hay:
                return False
        return True
