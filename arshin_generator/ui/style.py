from PySide6.QtGui import QColor, QPalette

INK = "#26313c"
MUTED = "#536172"
SURFACE = "#ffffff"
CANVAS = "#f4f5f7"
STRIPE = "#f8f9fb"
HEADER = "#e9edf2"
BORDER = "#bac4d0"
ACCENT = "#304e70"
ACCENT_HOVER = "#294462"
ACCENT_PRESSED = "#223953"
FOCUS = "#4e729e"
SELECTION = "#dde7f3"
HOVER = "#edf2f8"
DISABLED_SURFACE = "#eceff2"
DISABLED_INK = "#606b78"
SUCCESS = "#2c5069"
WARNING = "#755619"
ERROR_TEXT = "#9c2a31"

STYLE = f"""
QWidget {{ font: 10pt "Segoe UI"; }}
QMainWindow, QDialog, QScrollArea {{ background: {CANVAS}; color: {INK}; }}
QScrollArea {{ border: 1px solid {BORDER}; }}
QLabel#heading {{ color: {INK}; font-size: 12pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#workTitle {{ color: {INK}; font-size: 16pt; font-weight: 600; }}

QPushButton, QToolButton {{
    color: {INK}; background: {SURFACE}; border: 1px solid {BORDER};
    border-radius: 2px; padding: 5px 10px; min-height: 18px;
}}
QPushButton:hover, QToolButton:hover {{
    background: {HOVER}; border-color: {FOCUS};
}}
QPushButton:pressed, QToolButton:pressed {{ background: {SELECTION}; }}
QPushButton:focus, QToolButton:focus, QPushButton:default {{ border-color: {FOCUS}; }}
QPushButton#primary {{
    color: {SURFACE}; background: {ACCENT}; border-color: {ACCENT}; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#primary:focus {{ border-color: {SURFACE}; outline: 1px solid {FOCUS}; }}
QPushButton:disabled, QToolButton:disabled, QPushButton#primary:disabled {{
    color: {DISABLED_INK}; background: {DISABLED_SURFACE}; border-color: {BORDER};
}}

QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{
    color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 2px;
    padding: 4px; selection-background-color: {SELECTION}; selection-color: {INK};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {FOCUS}; }}
QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    color: {DISABLED_INK}; background: {DISABLED_SURFACE};
}}
QComboBox QAbstractItemView {{
    color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; outline: 0;
    selection-background-color: {SELECTION}; selection-color: {INK};
}}
QComboBox QAbstractItemView::item {{ min-height: 23px; padding: 1px 5px; }}
QComboBox QAbstractItemView::item:hover {{ color: {INK}; background: {HOVER}; }}
QComboBox QAbstractItemView::item:selected {{ color: {INK}; background: {SELECTION}; }}

QAbstractItemView {{
    color: {INK}; background: {SURFACE}; alternate-background-color: {STRIPE};
    border: 1px solid {BORDER}; selection-background-color: {SELECTION}; selection-color: {INK};
}}
QTableView {{ gridline-color: {HEADER}; }}
QHeaderView::section {{
    color: {INK}; background: {HEADER}; padding: 5px 7px; border: 0;
    border-bottom: 1px solid {BORDER}; font-weight: 600;
}}
QHeaderView#protocolHeader::section {{ padding-left: 2px; padding-right: 2px; }}
QHeaderView#previewHeader::section {{ padding-left: 4px; padding-right: 4px; }}
QTableCornerButton::section {{ background: {HEADER}; border: 0; }}
QTabBar::tab {{
    color: {MUTED}; background: {HEADER}; padding: 5px 10px; margin-right: 2px;
    border: 1px solid transparent; border-radius: 2px;
}}
QTabBar::tab:hover {{ color: {INK}; background: {HOVER}; }}
QTabBar::tab:selected {{ color: {INK}; background: {SELECTION}; font-weight: 600; }}
QTabBar::tab:focus {{ border-color: {FOCUS}; }}
QTabWidget::pane {{ border: 0; top: -1px; }}
QSplitter::handle {{ background: {HEADER}; margin: 3px 1px; }}

QMenu, QToolTip {{ color: {INK}; background: {SURFACE}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 4px 18px; }}
QMenu::item:selected {{ color: {INK}; background: {SELECTION}; }}
QMenu::item:disabled {{ color: {DISABLED_INK}; }}
"""


def apply_theme(app):
    app.setStyle("Fusion")
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
