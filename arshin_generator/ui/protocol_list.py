from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHeaderView, QStyle

from .common import TableModel


class ProtocolTableModel(TableModel):
    marks_changed = Signal()

    def __init__(self, headers, parent=None):
        super().__init__(headers, parent)
        self.marked_paths = set()

    def update(self, rows):
        self.marked_paths.intersection_update(protocol.path for protocol, _ in rows)
        super().update(rows)
        self.marks_changed.emit()

    def data(self, index, role):
        if index.isValid() and index.column() == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return (Qt.CheckState.Checked if self.rows[index.row()][0].path in self.marked_paths
                        else Qt.CheckState.Unchecked)
            return None
        return super().data(index, role)

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        path = self.rows[index.row()][0].path
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.marked_paths.add(path)
        else:
            self.marked_paths.discard(path)
        self.dataChanged.emit(index, index, [role])
        self.marks_changed.emit()
        return True

    def set_all_checked(self, checked):
        self.marked_paths = {protocol.path for protocol, _ in self.rows} if checked else set()
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0),
                                  [Qt.ItemDataRole.CheckStateRole])
        self.marks_changed.emit()

    def all_check_state(self):
        if not self.marked_paths:
            return Qt.CheckState.Unchecked
        return (Qt.CheckState.Checked if len(self.marked_paths) == len(self.rows)
                else Qt.CheckState.PartiallyChecked)


class SelectAllCheckBox(QCheckBox):
    def nextCheckState(self):
        self.setCheckState(Qt.CheckState.Unchecked if self.checkState() == Qt.CheckState.Checked
                           else Qt.CheckState.Checked)


class ProtocolHeader(QHeaderView):
    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.select_all = SelectAllCheckBox(self.viewport())
        self.select_all.setTristate(True)
        self.select_all.setAccessibleName("Выбрать все")
        self.select_all.setToolTip("Выбрать все")
        self.select_all.setFixedSize(self.select_all.sizeHint())
        self.geometriesChanged.connect(self.place_checkbox)
        self.sectionResized.connect(self.place_checkbox)
        parent.horizontalScrollBar().valueChanged.connect(self.place_checkbox)

    def place_checkbox(self, *_):
        margin = self.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin) + 1
        top = (self.height() - self.select_all.height()) // 2
        self.select_all.move(self.sectionViewportPosition(0) + margin, top)
