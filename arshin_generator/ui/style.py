from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

INK = "#172f3e"
MUTED = "#415b68"
SURFACE = "#ffffff"
CANVAS = "#f2f6f7"
STRIPE = "#f4f8fa"
HEADER = "#e7f0f3"
BORDER = "#b9cdd5"
ACCENT = "#0c6557"
ACCENT_HOVER = "#095549"
ACCENT_PRESSED = "#073e36"
FOCUS = "#268577"
SELECTION = "#cde9e4"
HOVER = "#e5f3f0"
DISABLED_SURFACE = "#eaf0f2"
DISABLED_INK = "#526773"
SUCCESS = "#085641"
WARNING = "#81371c"
MAST = "#102b38"
ON_MAST = "#f3fbfb"
MAST_SECONDARY = "#c0d8dd"
MAST_SUMMARY = "#a7ebe0"

STYLE = f"""
QWidget {{ font: 10pt "Segoe UI"; }}
QMainWindow, QDialog, QScrollArea {{ background: {CANVAS}; color: {INK}; }}
QScrollArea {{ border: 1px solid {BORDER}; border-radius: 5px; }}
QFileDialog {{ min-width: 750px; min-height: 440px; }}

QFrame#mast {{ background: {MAST}; border-radius: 12px; }}
QLabel#title {{ color: {ON_MAST}; font-size: 23pt; font-weight: 600; }}
QLabel#subtitle {{ color: {MAST_SECONDARY}; font-size: 10pt; }}
QLabel#summary {{ color: {MAST_SUMMARY}; font-size: 12pt; font-weight: 600; }}
QLabel#heading {{ color: {INK}; font-size: 14pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#workTitle {{ color: {INK}; font-size: 23pt; font-weight: 600; }}
QFrame#emptyQueue {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 9px; }}
QPushButton#rowAction {{ margin: 4px 6px; padding: 5px 10px; }}

QPushButton, QToolButton {{
    color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 7px 12px; font-weight: 600; min-height: 18px;
}}
QPushButton:hover, QToolButton:hover {{
    color: {INK}; background: {HOVER}; border-color: {FOCUS};
}}
QPushButton:pressed, QToolButton:pressed {{ color: {INK}; background: {SELECTION}; }}
QPushButton:focus, QToolButton:focus, QPushButton:default {{ border-color: {FOCUS}; }}
QPushButton#primary {{ color: {SURFACE}; background: {ACCENT}; border-color: {ACCENT}; }}
QPushButton#primary:hover {{ color: {SURFACE}; background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ color: {SURFACE}; background: {ACCENT_PRESSED}; }}
QPushButton#primary:focus {{ border-color: {ON_MAST}; outline: 1px solid {FOCUS}; }}
QPushButton:disabled, QToolButton:disabled, QPushButton#primary:disabled {{
    color: {DISABLED_INK}; background: {DISABLED_SURFACE}; border-color: {BORDER};
}}

QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{
    color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px;
    padding: 6px; selection-background-color: {SELECTION}; selection-color: {INK};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {FOCUS}; }}
QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    color: {DISABLED_INK}; background: {DISABLED_SURFACE};
}}
QComboBox QAbstractItemView {{
    color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; outline: 0;
    selection-background-color: {SELECTION}; selection-color: {INK};
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 2px 6px; }}
QComboBox QAbstractItemView::item:hover {{ color: {INK}; background: {HOVER}; }}
QComboBox QAbstractItemView::item:selected {{ color: {INK}; background: {SELECTION}; }}

QAbstractItemView {{
    color: {INK}; background: {SURFACE}; alternate-background-color: {STRIPE};
    border: 1px solid {BORDER}; selection-background-color: {SELECTION}; selection-color: {INK};
}}
QTableView {{ gridline-color: {HEADER}; }}
QHeaderView::section {{
    color: {INK}; background: {HEADER}; padding: 8px; border: 0;
    border-bottom: 1px solid {BORDER}; font-weight: 600;
}}
QFileDialog QHeaderView::section {{ padding: 5px 3px; }}
QTableCornerButton::section {{ background: {HEADER}; border: 0; }}
QTabBar::tab {{
    color: {MUTED}; background: {HEADER}; padding: 8px 14px; margin-right: 3px;
    border: 1px solid transparent; border-radius: 4px;
}}
QTabBar::tab:hover {{ color: {INK}; background: {HOVER}; }}
QTabBar::tab:selected {{ color: {INK}; background: {SELECTION}; font-weight: 600; }}
QTabBar::tab:focus {{ border-color: {FOCUS}; }}
QTabWidget::pane {{ border: 0; top: -1px; }}
QSplitter::handle {{ background: {HEADER}; margin: 4px 2px; }}

QMenu, QToolTip {{ color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 6px 24px; }}
QMenu::item:selected {{ color: {INK}; background: {SELECTION}; }}
QMenu::item:disabled {{ color: {DISABLED_INK}; }}
"""


def apply_theme(app):
    app.setStyle("Fusion")
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    palette = QPalette()
    role = QPalette.ColorRole
    for key, color in {
        role.Window: CANVAS,
        role.WindowText: INK,
        role.Base: SURFACE,
        role.AlternateBase: STRIPE,
        role.Text: INK,
        role.Button: SURFACE,
        role.ButtonText: INK,
        role.PlaceholderText: MUTED,
        role.Highlight: SELECTION,
        role.HighlightedText: INK,
        role.Accent: ACCENT,
        role.ToolTipBase: SURFACE,
        role.ToolTipText: INK,
        role.Link: ACCENT,
        role.LinkVisited: ACCENT_PRESSED,
        role.Mid: BORDER,
    }.items():
        palette.setColor(key, QColor(color))
    for key in (role.WindowText, role.Text, role.ButtonText, role.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, key, QColor(DISABLED_INK))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)
