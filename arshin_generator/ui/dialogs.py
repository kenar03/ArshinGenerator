from dataclasses import replace
from getpass import getuser
import json
import logging
from pathlib import Path
import sqlite3

from lxml import etree
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..arshin import NS, open_xml
from ..registry import CATEGORIES, iso_date, lookup_key, resolve_many
from ..storage import PORTAL_STATES
from .common import Form, apply_dialog_icon, button, fit_window, label, message, selected, table

TAGS = {
    "application": "Пакет поверок", "result": "Результат поверки", "miInfo": "Сведения о СИ",
    "singleMI": "СИ", "etaMI": "СИ в качестве эталона", "mitypeNumber": "Регистрационный номер типа",
    "manufactureNum": "Заводской номер", "typeNum": "Тип по реестру", "inventoryNum": "Обозначение",
    "manufactureYear": "Год выпуска", "modification": "Модификация", "signCipher": "Шифр знака",
    "miOwner": "Владелец", "vrfDate": "Дата поверки", "validDate": "Действительна до",
    "type": "Вид поверки", "calibration": "Использование калибровки",
    "applicable": "Пригоден", "inapplicable": "Непригоден", "reasons": "Причина",
    "docTitle": "Методика поверки", "metrologist": "Поверитель", "stickerNum": "Наклейка",
    "signPass": "Знак в паспорте", "signMi": "Знак на приборе", "means": "Средства поверки",
    "mi": "Средство измерений", "number": "Номер записи", "conditions": "Условия поверки",
    "temperature": "Температура", "pressure": "Атмосферное давление", "hymidity": "Влажность",
    "other": "Другие факторы", "structure": "Состав СИ", "brief_procedure": "Сокращённый объём",
    "characteristics": "Характеристика", "additional_info": "Прочие сведения", "protocol": "Протокол",
    "content": "Содержимое вложения", "ses": "Стандартные образцы", "se": "Стандартный образец",
    "oMethod": "Метод без средств поверки", "reagent": "Вещество или материал", **CATEGORIES,
}


def _value(tag, text):
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
    node = QTreeWidgetItem(parent, [title, _value(tag, value)])
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


class EquipmentReviewPanel(QWidget):
    changed = Signal()

    def __init__(self, parent, storage, protocol, lookups, *, checked_inputs=True):
        super().__init__(parent)
        self.storage = storage
        self.protocol = protocol
        self.lookups = lookups
        self.checked_inputs = checked_inputs
        day = protocol.fields.get("verification_date")
        self.day = iso_date(day.value if day else None)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.summary = label("")
        box.addWidget(self.summary)
        self.items, self.item_model = table(
            ("Средство поверки", "Заводской №", "Номер ФИФ ОЕИ", "Состояние"), ()
        )
        box.addWidget(self.items)
        self.note = label("")
        box.addWidget(self.note)
        self.candidates, self.candidate_model = table(
            ("Номер записи", "Модель", "Зав. №", "Поверена", "Действует до", "Схема / состояние"), ()
        )
        box.addWidget(self.candidates)
        for view in (self.items, self.candidates):
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.verticalHeader().setDefaultSectionSize(view.fontMetrics().height() + 10)
            view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            view.horizontalScrollBar().rangeChanged.connect(lambda *_args, view=view: self.fit_table(view))
        self.items.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.items.verticalHeader().setMinimumSectionSize(self.items.verticalHeader().defaultSectionSize())
        actions = QHBoxLayout()
        self.select_btn = button("Выбрать запись", self.choose, primary=True)
        actions.addWidget(self.select_btn)
        actions.addStretch()
        box.addLayout(actions)
        self.items.selectionModel().currentChanged.connect(self.refresh_candidates)
        self.candidates.selectionModel().selectionChanged.connect(self.refresh_button)
        self.refresh_items()

    @staticmethod
    def fit_table(view):
        height = view.horizontalHeader().sizeHint().height() + 2 * view.frameWidth()
        height += sum(view.rowHeight(row) for row in range(view.model().rowCount()))
        scrollbar = view.horizontalScrollBar()
        if scrollbar.maximum():
            height += scrollbar.sizeHint().height()
        view.setFixedHeight(height)

    def lookup(self, item):
        return self.lookups.get(lookup_key(item, self.day.isoformat())) if item and self.day else None

    def refresh_items(self, current=None):
        entries = self.storage.entries()
        rows = []
        self.assessments = {}
        for item in self.protocol.equipment:
            live = self.lookup(item)
            resolved, issue = resolve_many(item, entries, self.day, live, require_public=True) if self.checked_inputs else ([], "")
            self.assessments[item] = bool(resolved), issue
            numbers = ", ".join(entry.identifier or entry.type_number for entry in resolved)
            rows.append((item, (item.name or item.description, item.serial, numbers, "Проверено" if resolved else issue)))
        self.pending = [item for item, (ready, _) in self.assessments.items() if not ready]
        self.checked = sum(self.assessments[item][0] for item, _ in rows)
        suffix = f": проверено {self.checked} из {len(rows)}" if self.checked_inputs else ""
        self.summary.setText("Средства поверки" + suffix)
        self.item_model.update(rows)
        name_width = max(self.items.horizontalHeader().sectionSizeHint(0), self.items.sizeHintForColumn(0))
        self.items.setColumnWidth(0, min(390, name_width))
        self.items.resizeRowsToContents()
        self.fit_table(self.items)
        if rows:
            focus = current or (self.pending[0] if self.pending else None)
            index = next((i for i, row in enumerate(rows) if row[0] == focus), 0)
            self.items.selectRow(index)
        self.refresh_candidates()

    def refresh_candidates(self, *_):
        item = selected(self.items)
        live = self.lookup(item)
        resolved, issue = self.assessments.get(item, (False, ""))
        candidates = live.candidates if live else ()
        several = bool(item and item.category == "mieta" and sum(c.eligible for c in candidates) > 1)
        self.candidates.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection if several else
                                         QAbstractItemView.SelectionMode.SingleSelection)
        note = issue
        if several and not resolved:
            note += ". Нажмите на каждую запись, которая действительно использовалась."
        if live and live.state == "expired" and len(candidates) == 1 and candidates[0].entry.valid_until:
            end = iso_date(candidates[0].entry.valid_until)
            if end:
                note += f". Поверка действовала до {end:%d.%m.%Y}."
            if candidates[0].status.startswith(("Модель", "Заводской номер")):
                note += f" {candidates[0].status}."
        self.note.setText(note if live and (live.state == "choice" or live.state == "expired") else "")
        self.note.setVisible(bool(self.note.text()))
        eligible = [candidate for candidate in candidates if candidate.eligible]
        self.candidate_model.update([
            (c, (c.entry.identifier or c.entry.type_number, c.entry.model, c.entry.registry_serial, c.entry.valid_from,
                 c.entry.valid_until, " · ".join(filter(None, (c.status, c.entry.scheme)))))
            for c in eligible
        ])
        self.fit_table(self.candidates)
        self.candidates.setVisible(bool(not resolved and live and live.state in {"found", "choice"} and eligible))
        self.refresh_button()

    def refresh_button(self, *_):
        candidates = self.selected_candidates()
        item = selected(self.items)
        live = self.lookup(item)
        resolved, _ = self.assessments.get(item, (False, ""))
        choosing = bool(not resolved and live and live.state in {"found", "choice"} and self.candidate_model.rows)
        self.select_btn.setVisible(choosing)
        self.select_btn.setEnabled(bool(choosing and candidates and all(c.eligible for c in candidates)))

    def selected_candidates(self):
        indexes = self.candidates.selectionModel().selectedRows()
        return [self.candidate_model.rows[index.row()][0] for index in sorted(indexes, key=lambda i: i.row())]

    def choose(self):
        candidates = self.selected_candidates()
        if not candidates or not all(candidate.eligible for candidate in candidates):
            return
        try:
            self.storage.save_equipment_choices([
                replace(c.entry, evidence=f"{c.entry.evidence} · Выбрано по протоколу", confirmer=getuser())
                for c in candidates
            ])
        except (ValueError, sqlite3.Error) as exc:
            self.note.setText(str(exc) if isinstance(exc, ValueError) else "Не удалось сохранить выбор.")
            self.note.show()
            return
        self.changed.emit()


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
