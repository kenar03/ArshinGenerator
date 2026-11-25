from datetime import date, datetime
import sys

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QTableView, QVBoxLayout, QWidget,
    QAbstractItemView, QApplication, QHeaderView,
)

from ..domain import CORRECTION, ERROR, READY, REVIEW, UNSUPPORTED
from .style import ERROR_TEXT, SUCCESS, WARNING


def button(label, slot=None, primary=False):
    widget = QPushButton(label)
    if slot:
        widget.clicked.connect(slot)
    if primary:
        widget.setObjectName("primary")
    return widget


def label(text, style="muted"):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(style)
    widget.setWordWrap(True)
    return widget


def apply_dialog_icon(dialog):
    dialog.setWindowIcon(QApplication.windowIcon())
    if sys.platform == "win32":
        dialog.setWindowFlag(Qt.WindowType.WindowSystemMenuHint)


def message(parent, title, detail, *, ask=False, error=False, confirm_text="Продолжить"):
    dialog = QMessageBox(parent)
    apply_dialog_icon(dialog)
    dialog.setWindowTitle(title)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(detail)
    icon = QMessageBox.Icon.Question if ask else QMessageBox.Icon.Warning if error else QMessageBox.Icon.Information
    dialog.setIcon(icon)
    if ask:
        yes = dialog.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
        no = dialog.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(no)
    else:
        yes = dialog.addButton("Закрыть", QMessageBox.ButtonRole.AcceptRole)
    dialog.exec()
    return dialog.clickedButton() is yes


def display(value):
    if value is True:
        return "Да"
    if value is False:
        return "Нет"
    return str(value) if value not in (None, "") else "—"


class TableModel(QAbstractTableModel):
    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.headers = headers
        self.rows = []

    def update(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role):
        if not index.isValid():
            return None
        value = self.rows[index.row()][1][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return display(value)
        if role == Qt.ItemDataRole.ForegroundRole:
            if value == READY:
                return QColor(SUCCESS)
            if value == REVIEW:
                return QColor(WARNING)
            if value in (CORRECTION, ERROR, UNSUPPORTED):
                return QColor(ERROR_TEXT)
        return None


def table(headers, widths, *, model_class=TableModel, header_class=None):
    view = QTableView()
    if header_class:
        view.setHorizontalHeader(header_class(view))
    model = model_class(headers, view)
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(30)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for column, width in enumerate(widths):
        view.setColumnWidth(column, width)
    return view, model


def selected(view):
    index = view.currentIndex()
    model = view.model()
    return model.rows[index.row()][0] if index.isValid() and index.row() < len(model.rows) else None


def fit_window(widget, width, height):
    area = widget.screen().availableGeometry()
    widget.resize(min(width, area.width() - 24), min(height, area.height() - 52))


class Fields(QWidget):
    def __init__(self):
        super().__init__()
        self.form = QFormLayout(self)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setVerticalSpacing(9)
        self.controls = {}
        self.date_fields = set()

    def field(self, key, title, kind="text", value=None, choices=None, readonly=False):
        if choices is not None or kind in ("bool", "choice", "outcome"):
            choices = choices or {
                "bool": [("Не указано", None), ("Да", True), ("Нет", False)],
                "choice": [("Не указано", ""), ("Первичная", "1"), ("Периодическая", "2")],
                "outcome": [("Не подтверждён", ""), ("Пригоден", "pass"), ("Непригоден", "fail")],
            }[kind]
            control = QComboBox()
            for name, data in choices:
                control.addItem(name, data)
            index = control.findData(value)
            if index >= 0:
                control.setCurrentIndex(index)
            control.setEnabled(not readonly)
        elif kind == "list":
            control = QPlainTextEdit(str(value or ""))
            control.setMaximumHeight(100)
            control.setReadOnly(readonly)
            control.setPlaceholderText("Каждый номер с новой строки или через запятую")
        else:
            control = QLineEdit()
            control.setMaxLength(2048)
            control.setReadOnly(readonly)
            if kind == "date":
                self.date_fields.add(key)
                control.setPlaceholderText("ДД.ММ.ГГГГ")
        self.controls[key] = control
        if isinstance(control, QLineEdit):
            self.set_value(key, value)
        self.form.addRow(title, control)
        return control

    def values(self):
        answer = {}
        for key, control in self.controls.items():
            if isinstance(control, QComboBox):
                answer[key] = control.currentData()
            elif isinstance(control, QPlainTextEdit):
                answer[key] = control.toPlainText().strip()
            else:
                content = control.text().strip()
                if key in self.date_fields and "." in content:
                    try:
                        content = datetime.strptime(content, "%d.%m.%Y").date().isoformat()
                    except ValueError:
                        pass
                answer[key] = content
        return answer

    def set_value(self, key, value):
        control = self.controls[key]
        if isinstance(control, QComboBox):
            control.setCurrentIndex(max(0, control.findData(value)))
        elif isinstance(control, QPlainTextEdit):
            control.setPlainText(str(value or ""))
        else:
            content = str(value or "")
            if key in self.date_fields and content:
                try:
                    content = date.fromisoformat(content).strftime("%d.%m.%Y")
                except ValueError:
                    pass
            control.setText(content)


class Form(QDialog):
    def __init__(self, parent, title, guidance="", *, size=(610, 700), save="Сохранить"):
        super().__init__(parent)
        apply_dialog_icon(self)
        self.setWindowTitle(title)
        fit_window(self, *size)
        self.editor = Fields()
        self.validate = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.addWidget(label(title, "heading"))
        if guidance:
            outer.addWidget(label(guidance))
        scroll = QScrollArea()
        scroll.setWidget(self.editor)
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("Отмена", self.reject))
        confirm = button(save, self._accept, primary=True)
        confirm.setDefault(True)
        actions.addWidget(confirm)
        outer.addLayout(actions)

    def field(self, key, title, kind="text", value=None, choices=None, readonly=False):
        return self.editor.field(key, title, kind, value, choices, readonly)

    def values(self):
        return self.editor.values()

    def _accept(self):
        try:
            if self.validate:
                self.validate(self.values())
        except ValueError as exc:
            message(self, "Проверьте введённые данные", str(exc), error=True)
            return
        self.accept()
