import json
import logging
from pathlib import Path
import sqlite3
from urllib.parse import quote

from lxml import etree
from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..arshin import NS, open_xml
from ..domain import RegistryEntry
from ..public_registry import PUBLIC_CARD, find_public_verifications
from ..registry import CATEGORIES, registry_issues
from ..storage import PORTAL_STATES
from .common import Form, button, confirmation_values, label, message, selected, table

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
        self.setWindowTitle("Просмотр и проверка XML")
        self.resize(1060, 690)
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
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("Закрыть", self.accept))
        layout.addLayout(actions)
        self.results.selectionModel().currentChanged.connect(self.card)
        self.search.textChanged.connect(self.filter)
        selector.currentIndexChanged.connect(lambda: self.load(selector.currentData()))
        self.load(paths[0])

    def load(self, path):
        self.records = []
        self.raw.clear()
        try:
            root, issues, details = open_xml(path, self.schema)
            self.raw.setPlainText(etree.tostring(root, encoding="unicode", pretty_print=True))
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


class RegistryDialog(QDialog):
    def __init__(self, parent, storage, items=()):
        super().__init__(parent)
        self.setWindowTitle("Справочник средств поверки")
        self.resize(1080, 630)
        self.storage = storage
        self.items = items
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.addWidget(label("Справочник средств поверки", "heading"))
        box.addWidget(label("Локальная база подтверждений. Онлайн-проверка не выполнена. "
                            "Сверьте конкретную единицу и даты поверки в официальной карточке."))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти по названию, номеру или описанию")
        row.addWidget(self.search, 1)
        row.addWidget(button("Открыть ФИФ ОЕИ", self.portal))
        row.addWidget(button("Поиск опубликованных поверок", self.public_search))
        box.addLayout(row)
        self.tabs = QTabWidget()
        self.known, self.known_model = table(
            ("Название", "Заводской №", "Категория", "Идентификатор / тип", "Действует до", "Основание"),
            (210, 105, 235, 155, 110)
        )
        self.tabs.addTab(self.known, "Подтверждённые")
        self.candidates, self.candidate_model = table(
            ("Из протокола", "№ в книге", "Заводской №", "Источник / срок по Excel"), (410, 155, 105)
        )
        self.tabs.addTab(self.candidates, "Из выбранного протокола")
        box.addWidget(self.tabs, 1)
        buttons = QHBoxLayout()
        self.add = button("Подтвердить средство", self.create, primary=True)
        self.edit = button("Изменить подтверждение", self.modify)
        self.archive = button("Отозвать", self.withdraw)
        buttons.addWidget(self.add)
        buttons.addWidget(self.edit)
        buttons.addWidget(self.archive)
        buttons.addStretch()
        buttons.addWidget(button("Закрыть", self.accept))
        box.addLayout(buttons)
        self.tabs.currentChanged.connect(self.refresh_controls)
        self.known.selectionModel().currentChanged.connect(self.refresh_controls)
        self.candidates.selectionModel().currentChanged.connect(self.refresh_controls)
        self.known.doubleClicked.connect(self.modify)
        self.candidates.doubleClicked.connect(self.create)
        self.search.textChanged.connect(self.refresh)
        self.tabs.setCurrentIndex(1 if self.items else 0)
        self.refresh()

    @staticmethod
    def portal():
        QDesktopServices.openUrl(QUrl("https://fgis.gost.ru/fundmetrology/cm/"))

    def public_search(self):
        PublicSearchDialog(self).exec()

    def refresh(self):
        term = self.search.text().strip().casefold()
        self.known_model.update([
            (e, (e.name, e.serial, CATEGORIES[e.category], e.identifier or e.type_number, e.valid_until, e.evidence))
            for e in self.storage.entries()
            if term in " ".join((e.name, e.serial, e.identifier, e.type_number, e.description)).casefold()
        ])
        self.candidate_model.update([
            (e, (e.description, e.source_id, e.serial, " · ".join(filter(None, (e.source, e.valid_hint)))))
            for e in self.items
            if term in " ".join((e.description, e.serial, e.source_id)).casefold()
        ])
        if self.known_model.rows:
            self.known.selectRow(0)
        if self.candidate_model.rows:
            self.candidates.selectRow(0)
        self.refresh_controls()

    def refresh_controls(self, *_):
        known = self.tabs.currentIndex() == 0
        self.edit.setEnabled(known and selected(self.known) is not None)
        self.archive.setEnabled(self.edit.isEnabled())
        self.add.setEnabled(known or selected(self.candidates) is not None)
        self.add.setText("Новое подтверждение" if known else "Подтвердить средство из протокола")

    def create(self, *_):
        item = selected(self.candidates) if self.tabs.currentIndex() == 1 else None
        self.editor(item=item)

    def modify(self, *_):
        existing = selected(self.known)
        if existing:
            self.editor(existing=existing)

    def editor(self, item=None, existing=None):
        form = Form(self, "Подтверждение средства поверки",
                    "Введите реквизиты конкретной карточки и срок по документу. "
                    "Для обычного СИ номер типа и номер единицы сверяются посимвольно.")
        values = (
            ("description", "Строка из протокола", "text"),
            ("local_id", "Номер из протокола / оборудования", "text"),
            ("serial", "Номер единицы в протоколе", "text"),
            ("name", "Название и модель", "text"),
            ("category", "Категория в ФИФ ОЕИ", "text"),
            ("identifier", "Номер конкретной записи эталона", "text"),
            ("type_number", "Регномер типа обычного СИ", "text"),
            ("registry_serial", "Номер или обозначение обычного СИ", "text"),
            ("identity_kind", "Вид номера обычного СИ", "text"),
            ("valid_from", "Дата поверки оборудования", "date"),
            ("valid_until", "Поверка оборудования действительна до", "date"),
        )
        starting = {
            "description": item.description, "local_id": item.source_id, "serial": item.serial, "name": item.name
        } if item else {}
        for key, title, kind in values:
            choices = None
            if key == "category":
                choices = [("Выберите категорию", ""), *((v, k) for k, v in CATEGORIES.items())]
            if key == "identity_kind":
                choices = [("Заводской номер", "manufactureNum"), ("Обозначение", "inventoryNum")]
            if key == "type_number" and item and item.source_id and "." not in item.source_id:
                starting[key] = item.source_id
                starting["registry_serial"] = item.serial
            form.field(key, title, kind, getattr(existing, key) if existing else starting.get(key), choices,
                       readonly=bool(item and key in ("description", "local_id", "serial") and starting.get(key)))
        form.confirmation(existing.confirmer if existing else self.storage.setting("confirmer"),
                          existing.evidence if existing else "")

        def entry_from(data):
            evidence, confirmer = confirmation_values(form, data)
            entry = RegistryEntry(**data, evidence=evidence, confirmer=confirmer, id=existing.id if existing else 0)
            errors = registry_issues(entry)
            if errors:
                raise ValueError("\n".join(errors))
            return entry

        form.validate = entry_from
        if form.exec():
            entry = entry_from(form.values())
            self.storage.save_equipment(entry)
            self.storage.set_setting("confirmer", entry.confirmer)
            self.refresh()

    def withdraw(self):
        item = selected(self.known)
        if not item:
            return
        form = Form(self, "Отзыв подтверждения", item.description, size=(550, 310), save="Отозвать")
        form.confirmation(self.storage.setting("confirmer"))
        form.validate = lambda data: confirmation_values(form, data)
        if form.exec():
            evidence, confirmer = confirmation_values(form, form.values())
            self.storage.archive_equipment(item, f"{confirmer}: {evidence}")
            self.refresh()


class RegistryLookup(QThread):
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, number, serial, parent):
        super().__init__(parent)
        self.number = number
        self.serial = serial

    def run(self):
        try:
            self.found.emit(find_public_verifications(self.number, self.serial))
        except ValueError as exc:
            self.failed.emit(str(exc))


class PublicSearchDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Опубликованные поверки ФИФ ОЕИ")
        self.resize(990, 530)
        self.worker = None
        layout = QVBoxLayout(self)
        layout.addWidget(label("Опубликованные поверки ФИФ ОЕИ", "heading"))
        layout.addWidget(label("Введите номер типа и заводской номер средства. Поиск передаёт эти два поля "
                               "в публичный реестр. Для «СИ в качестве эталона» сверьте № конкретной записи "
                               "по перечню в личном кабинете."))
        row = QHBoxLayout()
        self.kind = QLineEdit()
        self.kind.setPlaceholderText("Регистрационный № типа")
        self.number = QLineEdit()
        self.number.setPlaceholderText("Точный заводской номер")
        self.search = button("Найти в ФИФ ОЕИ", self.lookup, primary=True)
        for control in (self.kind, self.number, self.search):
            row.addWidget(control)
        layout.addLayout(row)
        self.notice = label("")
        layout.addWidget(self.notice)
        self.table, self.model = table(("Наименование", "Заводской №", "Дата поверки",
                                       "Действительна до", "Результат", "№ документа"),
                                      (250, 110, 125, 125, 105))
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("Открыть выбранную карточку", self.open))
        actions.addWidget(button("Закрыть", self.reject))
        layout.addLayout(actions)
        self.table.doubleClicked.connect(self.open)

    def lookup(self):
        if self.worker:
            return
        self.model.update([])
        self.notice.setText("Поиск опубликованных сведений…")
        self.search.setEnabled(False)
        self.worker = RegistryLookup(self.kind.text().strip(), self.number.text().strip(), self)
        self.worker.found.connect(self.show_results)
        self.worker.failed.connect(self.notice.setText)
        self.worker.finished.connect(self.finished_search)
        self.worker.start()

    def finished_search(self):
        self.worker.deleteLater()
        self.worker = None
        self.search.setEnabled(True)

    def reject(self):
        if self.worker:
            self.notice.setText("Дождитесь окончания запроса.")
        else:
            super().reject()

    def show_results(self, records):
        self.model.update([(record, (record.title, record.serial, record.verification_date,
                                    record.valid_date, record.outcome, record.document)) for record in records])
        self.notice.setText(f"Точных совпадений в ответе: {len(records)}. "
                            "Для экспорта требуется отдельное подтверждение соответствия.")
        if records:
            self.table.selectRow(0)

    def open(self, *_):
        record = selected(self.table)
        if record:
            QDesktopServices.openUrl(QUrl(PUBLIC_CARD + quote(record.record_id, safe="")))


class HistoryDialog(QDialog):
    def __init__(self, parent, storage, schema):
        super().__init__(parent)
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
        form = Form(self, "Статус по личному кабинету",
                    "Зафиксируйте фактический статус пакета и ссылку или номер заявки.", size=(560, 330))
        form.field("status", "Статус", value=row["status"], choices=[(x, x) for x in PORTAL_STATES])
        form.confirmation(self.storage.setting("confirmer"))
        form.validate = lambda data: confirmation_values(form, data)
        if form.exec():
            data = form.values()
            evidence, confirmer = confirmation_values(form, data)
            self.storage.change_status(row["id"], data["status"], evidence, confirmer)
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
