from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHeaderView, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .common import TableModel

ALIGNMENTS = (Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignLeft,
              Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignHCenter)


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
        if index.isValid() and role == Qt.ItemDataRole.TextAlignmentRole:
            return ALIGNMENTS[index.column()] | Qt.AlignmentFlag.AlignVCenter
        if index.isValid() and index.column() == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return (Qt.CheckState.Checked if self.rows[index.row()][0].path in self.marked_paths
                        else Qt.CheckState.Unchecked)
            return None
        return super().data(index, role)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            return ALIGNMENTS[section] | Qt.AlignmentFlag.AlignVCenter
        return super().headerData(section, orientation, role)

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
        self.setObjectName("protocolHeader")
        self.select_all = SelectAllCheckBox(self.viewport())
        self.select_all.setTristate(True)
        self.select_all.setAccessibleName("Выбрать все")
        self.select_all.setToolTip("Выбрать все")
        self.select_all.setFixedSize(self.select_all.sizeHint())
        self.geometriesChanged.connect(self.place_checkbox)
        self.sectionResized.connect(self.place_checkbox)
        parent.horizontalScrollBar().valueChanged.connect(self.place_checkbox)

    def place_checkbox(self, *_):
        margin = (self.sectionSize(0) - self.select_all.width()) // 2
        top = (self.height() - self.select_all.height()) // 2
        self.select_all.move(self.sectionViewportPosition(0) + margin, top)


class ProtocolCheckDelegate(QStyledItemDelegate):
    def check_options(self, option, index):
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        style = self.parent().style()
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, item, item.widget)
        return item, style, rect

    def paint(self, painter, option, index):
        item, style, rect = self.check_options(option, index)
        item.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter, item.widget)
        rect.moveCenter(item.rect.center())
        item.rect = rect
        item.state |= QStyle.StateFlag.State_On if item.checkState == Qt.CheckState.Checked else QStyle.StateFlag.State_Off
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck, item, painter, item.widget)

    def editorEvent(self, event, model, option, index):
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            item, _, rect = self.check_options(option, index)
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.translated(item.rect.center() - rect.center())
        return super().editorEvent(event, model, option, index)
