"""Dark theme QSS + shared color tokens."""
from __future__ import annotations

BG = "#1b1c22"
PANEL = "#23242c"
PANEL_ALT = "#2a2c36"
BORDER = "#383a46"
TEXT = "#e8e9ee"
DIM = "#9aa1b2"
ACCENT = "#5b8cff"
USER = "#5b8cff"
ASSISTANT = "#b18cff"
TOOL = "#d9a066"
REASONING = "#8b93a7"
OK = "#5ec269"
WARN = "#e0b04a"

ROLE_COLORS = {
    "user": USER,
    "assistant": ASSISTANT,
    "tool_call": TOOL,
    "tool_result": TOOL,
    "reasoning": REASONING,
    "system": DIM,
}

QSS = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QMainWindow {{ background: {BG}; }}
QToolBar {{ background: {PANEL}; border: 0; border-bottom: 1px solid {BORDER}; spacing: 4px; padding: 4px 8px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px 10px; color: {TEXT}; }}
QToolButton:hover {{ background: {PANEL_ALT}; border-color: {BORDER}; }}
QToolButton:pressed {{ background: {BORDER}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: #fff;
}}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; outline: 0; }}
QTableView {{ background: {BG}; border: 1px solid {BORDER}; gridline-color: transparent;
    alternate-background-color: #1f2027; selection-background-color: #2d3a63; selection-color: #fff; }}
QHeaderView::section {{ background: {PANEL}; color: {DIM}; border: 0; border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER}; padding: 6px 8px; }}
QTableView::item {{ padding: 4px 6px; }}
QSplitter::handle {{ background: {BORDER}; width: 1px; height: 1px; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #4a4d5c; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollArea {{ border: 0; background: transparent; }}
QLabel {{ background: transparent; }}
QDialog {{ background: {BG}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; color: {DIM}; padding: 6px 14px; border: 1px solid {BORDER};
    border-bottom: 0; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {PANEL_ALT}; color: {TEXT}; }}
QPushButton {{ background: {PANEL_ALT}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 6px 14px; color: {TEXT}; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: #666; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {DIM}; }}
QCheckBox {{ spacing: 6px; }}
QToolTip {{ background: {PANEL_ALT}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {PANEL_ALT}; }}
QFrame[msgframe="true"] {{ background: #202129; border: 1px solid {BORDER}; border-radius: 8px; }}
"""


def badge_style(role: str) -> str:
    c = ROLE_COLORS.get(role, DIM)
    return (f"background: rgba(255,255,255,0); color: {c}; font-weight: 600;"
            f"border: 1px solid {c}; border-radius: 4px; padding: 0 5px;")
