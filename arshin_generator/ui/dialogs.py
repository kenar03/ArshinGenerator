import json
import logging
from pathlib import Path
import sqlite3

from lxml import etree
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..arshin import NS, open_xml
from ..registry import CATEGORIES
from ..storage import PORTAL_STATES
from .common import Form, apply_dialog_icon, button, fit_window, label, message, selected, table

TAGS = {
    "application": "Пакет поверок", "result": "Результат поверки", "miInfo": "Сведения о СИ",
    "singleMI": "СИ", "etaMI": "СИ в качестве эталона", "mitypeNumber": "Тип СИ в реестре ФИФ ОЕИ",
    "manufactureNum": "Заводской №", "typeNum": "Регистрационный № типа СИ",
    "inventoryNum": "Буквенно-цифровое обозначение", "manufactureYear": "Год выпуска",
    "modification": "Модификация СИ", "signCipher": "Условный шифр знака поверки",
    "miOwner": "Владелец СИ", "vrfDate": "Дата поверки", "validDate": "Действительна до",
    "type": "Тип поверки", "calibration": "Использование результатов калибровки",
    "applicable": "Пригодно", "inapplicable": "Непригодно", "reasons": "Причины непригодности",
    "docTitle": "Документ на методику поверки", "metrologist": "ФИО поверителя", "stickerNum": "Номер наклейки",
    "signPass": "Знак поверки в паспорте", "signMi": "Знак поверки на СИ", "means": "Средства поверки",
    "mi": "Средство измерений", "number": "Номер записи", "conditions": "Условия проведения поверки",
    "temperature": "Температура", "pressure": "Атмосферное давление", "hymidity": "Относительная влажность",
    "other": "Другие факторы", "structure": "Состав СИ, представленного на поверку",
    "brief_procedure": "Поверка в сокращенном объеме", "characteristics": "Краткая характеристика объема поверки",
    "additional_info": "Прочие сведения", "protocol": "Протокол",
    "content": "Содержимое вложения", "ses": "Стандартные образцы", "se": "Стандартный образец",
    "oMethod": "Метод без средств поверки", "reagent": "Вещество или материал", **CATEGORIES,
}


def field_text(tag, text):
    if tag in {"calibration", "signPass", "signMi", "isOwner"}:
        return {"true": "Да", "1": "Да", "false": "Нет", "0": "Нет"}.get(text, text)
    if tag == "type":
        return {"1": "Первичная", "2": "Периодическая"}.get(text, text)
    return text


def tree_items(element, parent):
    tag = etree.QName(element).localname
    title = TAGS.get(tag, tag)
    value = (element.text or "").strip()
    if tag == "content" and value:
        value = f"Вложение Base64 ({len(value)} символов)"
    node = QTreeWidgetItem(parent, [title, field_text(tag, value)])
    for key, text in element.attrib.items():
        QTreeWidgetItem(node, [key, text])
    for child in element:
        if isinstance(child.tag, str):
            tree_items(child, node)


class XmlDialog(QDialog):
    def __init__(self, parent, paths, schema, protocols=None):
        super().__init__(parent)
        apply_dialog_icon(self)
        self.setWindowTitle("Просмотр и проверка XML")
        fit_window(self, 930, 650)
        self.schema = schema
        self.protocols = protocols or {}
        self.paths = paths
        self.records = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(label("Просмотр и проверка XML", "heading"))
        row = QHBoxLayout()
        selector = QComboBox()
        for path in paths:
            selector.addItem(path.name, path)
        row.addWidget(selector, 2)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по заводскому № или номеру протокола")
        row.addWidget(self.search, 3)
        layout.addLayout(row)
        self.status = label("")
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        body = QWidget()
        body_layout = QHBoxLayout(body)
        self.results, self.model = table(("Заводской №", "Дата", "Заключение"), (115, 91))
        body_layout.addWidget(self.results, 2)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Поле", "Значение"])
        self.tree.setColumnWidth(0, 270)
        self.tree.setAlternatingRowColors(True)
        body_layout.addWidget(self.tree, 4)
        self.tabs.addTab(body, "Записи")
        technical = QWidget()
        technical_layout = QVBoxLayout(technical)
        finder = QHBoxLayout()
        finder.addWidget(label("Поиск использует текст из поля выше."))
        finder.addStretch()
        finder.addWidget(button("Найти далее", self.find_text))
        technical_layout.addLayout(finder)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.raw.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        technical_layout.addWidget(self.raw, 1)
        self.tabs.addTab(technical, "Текст XML")
        self.errors = QPlainTextEdit()
        self.errors.setReadOnly(True)
        self.tabs.addTab(self.errors, "Диагностика")
        self.tabs.setTabVisible(1, False)
        self.tabs.setTabVisible(2, False)
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        details = button("Технический режим")
        details.setCheckable(True)
        details.toggled.connect(self.toggle_technical)
        actions.addWidget(details)
        actions.addStretch()
        actions.addWidget(button("Закрыть", self.accept))
        layout.addLayout(actions)
        self.results.selectionModel().currentChanged.connect(self.card)
        self.search.textChanged.connect(self.filter)
        selector.currentIndexChanged.connect(lambda: self.load(selector.currentData()))
        self.load(paths[0])

    def toggle_technical(self, enabled):
        if not enabled:
            self.tabs.setCurrentIndex(0)
        self.tabs.setTabVisible(1, enabled)
        self.tabs.setTabVisible(2, enabled)

    def load(self, path):
        self.records = []
        self.raw.clear()
        try:
            root, issues, details = open_xml(path, self.schema)
            self.raw.setPlainText(etree.tostring(root.getroottree(), encoding="unicode", pretty_print=True))
            for record in root.findall(f"{{{NS}}}result"):
                serial_number = next(iter(record.xpath(
                    ".//*[local-name()='manufactureNum' or local-name()='inventoryNum' or local-name()='regNumber'][1]/text()"
                )), "")
                day = record.findtext(f"{{{NS}}}vrfDate", "")
                title = "Пригоден" if record.find(f"{{{NS}}}applicable") is not None else (
                    "Непригоден" if record.find(f"{{{NS}}}inapplicable") is not None else "Не определён"
                )
                type_number = record.findtext(f".//{{{NS}}}mitypeNumber", "")
                identity = (serial_number, day, type_number)
                self.records.append((record, (serial_number, day, title), self.protocols.get(identity, "")))
            summary = "Есть ошибки XML или размера." if issues else "XML соответствует выбранной XSD."
            self.status.setText(f"{summary}  Записей: {len(self.records)}.  {self.schema.path.name}")
            self.errors.setPlainText("\n".join(issues + (["", *details] if details else [])) or "Ошибок XSD нет.")
            if details:
                logging.warning("Проверка %s: %s", path, "\n".join(details))
        except (ValueError, OSError) as exc:
            self.status.setText("Файл не удалось открыть.")
            self.errors.setPlainText(str(exc))
        self.filter()

    def filter(self):
        term = self.search.text().strip().casefold()
        rows = []
        for record, values, protocol in self.records:
            info = record.findtext(f"{{{NS}}}additional_info", "")
            attachment = record.find(f"{{{NS}}}protocol")
            filename = attachment.get("filename", "") if attachment is not None else ""
            if not term or any(term in s.casefold() for s in (values[0], protocol, info, filename)):
                rows.append(((record, protocol), values))
        self.model.update(rows)
        self.tree.clear()
        if rows:
            self.results.selectRow(0)

    def card(self, *_):
        self.tree.clear()
        entry = selected(self.results)
        if entry:
            record, protocol = entry
            if protocol:
                QTreeWidgetItem(self.tree, ["Исходный протокол", protocol])
            for child in record:
                if isinstance(child.tag, str):
                    tree_items(child, self.tree)
            self.tree.expandToDepth(2)

    def find_text(self):
        term = self.search.text().strip()
        if term and not self.raw.find(term):
            self.raw.moveCursor(QTextCursor.MoveOperation.Start)
            if not self.raw.find(term):
                message(self, "Поиск по XML", "Фраза не найдена в тексте файла.")


class HistoryDialog(QDialog):
    def __init__(self, parent, storage, schema):
        super().__init__(parent)
        apply_dialog_icon(self)
        self.setWindowTitle("История XML")
        self.resize(960, 560)
        self.storage = storage
        self.schema = schema
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(label("История XML", "heading"))
        self.table, self.model = table(("Дата формирования", "Файлов", "Статус", "Папка"), (170, 68, 210))
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Открыть XML", self.open))
        row.addWidget(button("Подтвердить статус в кабинете", self.status))
        row.addWidget(button("Резервная копия базы", self.backup))
        row.addStretch()
        row.addWidget(button("Закрыть", self.accept))
        layout.addLayout(row)
        self.table.doubleClicked.connect(self.open)
        self.refresh()

    def refresh(self):
        self.model.update([
            (r, (r["created"][:19].replace("T", " "), len(json.loads(r["files"])), r["status"], r["directory"]))
            for r in self.storage.history()
        ])
        if self.model.rows:
            self.table.selectRow(0)

    def open(self, *_):
        row = selected(self.table)
        if row:
            paths = [Path(row["directory"]) / name for name in json.loads(row["files"])]
            XmlDialog(self, paths, self.schema, self.storage.export_context(row["id"])).exec()

    def status(self):
        row = selected(self.table)
        if not row:
            return
        form = Form(self, "Статус пакета", "Укажите фактический статус из личного кабинета.",
                    size=(540, 300), save="Подтвердить")
        form.field("status", "Статус", value=row["status"], choices=[(state, state) for state in PORTAL_STATES])
        form.field("evidence", "Ссылка или номер заявки")
        form.field("confirmer", "Сотрудник", value=self.storage.setting("confirmer"))

        def validate(values):
            if not values["evidence"] or not values["confirmer"]:
                raise ValueError("Укажите подтверждение из личного кабинета и имя сотрудника.")

        form.validate = validate
        if form.exec():
            values = form.values()
            try:
                self.storage.change_status(row["id"], values["status"], values["evidence"], values["confirmer"])
            except (ValueError, sqlite3.Error) as exc:
                message(self, "Статус пакета", str(exc), error=True)
            else:
                self.refresh()

    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить резервную копию", "arshin-backup.sqlite3",
                                              "Резервная копия SQLite (*.sqlite3)")
        if path:
            try:
                self.storage.backup(Path(path))
            except (ValueError, OSError, sqlite3.Error) as exc:
                message(self, "Резервная копия", str(exc), error=True)
            else:
                message(self, "Резервная копия", "Резервная копия создана.")
