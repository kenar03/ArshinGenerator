from collections import Counter
from getpass import getuser
import logging
import re
import sqlite3

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QScrollArea, QTreeWidget, QVBoxLayout, QWidget,
)

from ..arshin import NS, result_element
from ..confirmations import get_confirmation, individual_key, protocol_key, with_guards
from ..domain import OUTCOMES
from ..laboratory import DEFAULT_FIELDS, expiry_rule, validate_defaults
from ..normalization import metrologist, serial_list
from ..public_registry import check_connection
from ..registry import iso_date
from ..validation import (
    INDIVIDUAL_FIELDS, INVALID_STICKER, SHARED_FIELDS, modification_key, valid_modification, valid_sticker,
)
from .common import Fields, Form, button, fit_window, label, message, selected, table
from .dialogs import EquipmentReviewPanel, tree_items

PARTY_FIELDS = {"valid_date", "valid_date_absent", "sign_pass", "sign_mi"}
SIGN_FIELDS = {"sign_pass", "sign_mi"}
COMMON_FIELDS = (*SHARED_FIELDS, ("modification", "Модификация по описанию типа", "text"))
PARTY_ISSUES = (
    "Укажите дату окончания", "Дата окончания должна", "Подтверждение отсутствия срока",
    "Пропуск даты окончания", "Подтверждение: укажите знаки",
)


def readable(issue: str) -> str:
    issue = re.sub(r"[«»]?\b(?:Таблицы|Данные|Оборудование|Отчёт)[»]?\s*!\s*\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?",
                   "исходном протоколе", issue)
    issue = re.sub(r"\s*\(исходном протоколе\)", "", issue)
    return issue


def display_day(value) -> str:
    day = iso_date(value)
    return day.strftime("%d.%m.%Y") if day else ""


def correction_rows(confirmations, protocol, values, instrument=None):
    scope = "instrument" if instrument else "protocol"
    key = individual_key(protocol, instrument.serial) if instrument else protocol_key(protocol)
    prior = get_confirmation(confirmations, scope, protocol, instrument)
    fields = dict(prior[0]) if prior else {}
    values = dict(values)
    modification = values.pop("modification", "")
    fields.update(values)
    if values:
        yield (scope, key, with_guards(protocol, fields, instrument),
               "Уточнено пользователем по документам поверки", getuser())
    if modification:
        yield ("modification", modification_key(protocol), {"modification": modification},
               "Модификация подтверждена пользователем", getuser())


class RepairDialog(QDialog):
    def __init__(self, window, protocol):
        super().__init__(window)
        self.window = window
        self.protocol = protocol
        self.party_draft = {}
        self.individual_drafts = {}
        self.sign_exceptions = False
        self.setWindowTitle("Уточнение протокола")
        fit_window(self, 1000, 715)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(label(protocol.number or protocol.path.name, "heading"))
        self.status = label("")
        layout.addWidget(self.status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        layout.addWidget(self.scroll, 1)
        actions = QHBoxLayout()
        remove = button("Убрать из списка", self.remove_protocol)
        remove.setAutoDefault(False)
        actions.addWidget(remove)
        actions.addStretch()
        self.save_btn = button("Сохранить изменения", self.save, primary=True)
        actions.addWidget(self.save_btn)
        self.close_btn = button("Закрыть", self.accept)
        actions.addWidget(self.close_btn)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self):
        p = self.protocol
        records = self.window.group[id(p)]
        self.records = records
        good = sum(not record.blockers for record in records)
        status = f"Найдено приборов: {len(records)}    ·    Готово к выгрузке: {good}"
        page = QWidget()
        body = QVBoxLayout(page)
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(12)
        self.party_editor = None
        self.item_editor = None
        self.active_record = None
        self.equipment_panel = None
        common = set(records[0].missing_fields) if records else set()
        common.update(key for record in records for key in record.repair_fields & PARTY_FIELDS)
        equipment = records[0].equipment_issues if records else ()
        shared = [issue for issue in records[0].shared_issues if issue not in equipment] if records else p.issues
        shared.extend(dict.fromkeys(issue for record in records for issue in record.issues
                                    if issue.startswith(PARTY_ISSUES)))
        if p.error:
            shared = [p.error]
        self.add_party_fields(body, records, common, shared)
        if p.equipment or equipment:
            self.equipment_panel = EquipmentReviewPanel(self, self.window.storage, p, self.window.lookups)
            self.equipment_panel.changed.connect(self.changed_equipment)
            self.equipment_panel.retry.connect(self.retry)
            self.equipment_panel.summary.hide()
            body.addWidget(label("Средства поверки", "heading"))
            body.addWidget(self.equipment_panel)
            status += f"    ·    Средства поверки: {self.equipment_panel.checked} из {len(p.equipment)} проверено"
            for issue in equipment:
                if not self.equipment_panel.pending or not p.equipment:
                    body.addWidget(label(readable(issue)))
        if records:
            self.add_instrument_fields(body, records)
        for notice in p.notices:
            body.addWidget(label(readable(notice)))
        body.addStretch()
        self.scroll.setWidget(page)
        self.status.setText(status)
        editing = bool(self.party_editor or self.item_editor)
        self.save_btn.setVisible(editing)
        (self.save_btn if editing else self.close_btn).setDefault(True)

    def add_party_fields(self, box, records, common, issues):
        editable = [(key, title, kind) for key, title, kind in COMMON_FIELDS if key in common]
        if not (editable or issues):
            return
        count = len(editable) - int({"valid_date", "valid_date_absent"} <= common)
        title = f"Уточните сведения партии · полей: {count}" if editable else "Замечания к протоколу"
        box.addWidget(label(title, "heading"))
        listed = [readable(issue) for issue in dict.fromkeys(issues)
                  if not editable or not issue.startswith("Подтверждение:")]
        if listed:
            box.addWidget(label("\n".join(f"• {issue}" for issue in listed)))
        if not editable:
            return
        self.party_editor = Fields()
        current = records[0]
        for key, title, kind in editable:
            self.party_editor.field(key, title, kind, current.value(key)).setToolTip(
                self.protocol.proposed_modification if key == "modification" else "")
        self.party_initial = self.party_editor.values()
        for key, value in self.party_draft.items():
            if key in self.party_editor.controls:
                self.party_editor.set_value(key, value)
        box.addWidget(self.party_editor)

    def add_instrument_fields(self, box, records):
        measurements = Counter(readable(issue) for record in records for issue in record.measurement_issues)
        details = Counter(readable(issue) for record in records for issue in record.issues
                          if not issue.startswith(PARTY_ISSUES) and issue not in record.measurement_issues)
        for title, issues in (("Измерения и заключения", measurements), ("Реквизиты приборов", details)):
            if issues:
                box.addWidget(label(title, "heading"))
                box.addWidget(label("\n".join(f"• {issue} · приборов: {count}" for issue, count in issues.items())))
        exceptions = QCheckBox("Указать исключения по знакам отдельных приборов")
        exceptions.setChecked(self.sign_exceptions)
        exceptions.toggled.connect(self.toggle_sign_exceptions)
        box.addWidget(exceptions)
        individual_keys = {key for key, _, _ in INDIVIDUAL_FIELDS} - PARTY_FIELDS
        if self.sign_exceptions:
            individual_keys |= SIGN_FIELDS
        editable = [record for record in records if record.repair_fields & individual_keys or
                    (self.sign_exceptions and record.value("outcome") == "pass")]
        if not editable:
            return
        select = QComboBox()
        for record in editable:
            select.addItem(f"Прибор {record.instrument.serial}", record)
        row = QHBoxLayout()
        row.addWidget(select, 1)
        row.addWidget(button("Следующий прибор", lambda: select.setCurrentIndex((select.currentIndex() + 1) % select.count())))
        box.addLayout(row)
        area = QVBoxLayout()
        box.addLayout(area)

        def display():
            self.collect_instrument()
            if self.item_editor:
                area.removeWidget(self.item_editor)
                self.item_editor.deleteLater()
            record = select.currentData()
            keys = record.repair_fields & individual_keys
            if self.sign_exceptions and record.value("outcome") == "pass":
                keys |= SIGN_FIELDS
            if "outcome" in keys:
                keys.add("reasons")
            self.item_editor = Fields()
            for key, title, kind in INDIVIDUAL_FIELDS:
                if key in keys:
                    self.item_editor.field(key, title, kind, record.value(key))
            self.item_initial = self.item_editor.values()
            self.active_record = record
            for key, value in self.individual_drafts.get(record.instrument.serial, {}).items():
                if key in self.item_editor.controls:
                    self.item_editor.set_value(key, value)
            area.addWidget(self.item_editor)

        select.currentIndexChanged.connect(display)
        display()

    def collect_instrument(self):
        if self.item_editor and self.active_record:
            key = self.active_record.instrument.serial
            draft = self.individual_drafts.setdefault(key, {})
            for field, value in self.item_editor.values().items():
                if value != self.item_initial[field]:
                    draft[field] = value
                else:
                    draft.pop(field, None)

    def collect(self):
        self.collect_instrument()
        if self.party_editor:
            self.party_draft = {key: value for key, value in self.party_editor.values().items()
                                if value != self.party_initial[key]}

    def toggle_sign_exceptions(self, checked):
        self.collect()
        self.sign_exceptions = checked
        self.refresh()

    def changed_equipment(self):
        self.collect()
        self.window.recheck()
        self.refresh()

    def save(self):
        self.collect()
        p = self.protocol
        values = self.party_draft
        try:
            validate_defaults(values)
            if values.get("metrologist"):
                metrologist(values["metrologist"])
            if values.get("valid_date") and not iso_date(values["valid_date"]):
                raise ValueError("Введите дату в формате ДД.ММ.ГГГГ.")
            if values.get("modification") and not valid_modification(p, values["modification"]):
                raise ValueError("Уточните написание модификации по описанию типа.")
            if values.get("explicit_serials") and serial_list(values["explicit_serials"], explicit=True) != [
                i.serial for i in p.instruments
            ]:
                raise ValueError("Точный список должен совпасть с измеренными приборами.")
            records = {record.instrument.serial: record for record in self.records}
            for serial, changes in self.individual_drafts.items():
                if not valid_sticker(changes.get("sticker")):
                    raise ValueError(f"Прибор {serial}. {INVALID_STICKER}")
                if changes.get("outcome") == "pass" and records[serial].candidate == "fail":
                    raise ValueError(f"Прибор {serial}. Пригодность противоречит измерениям.")
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        storage = self.window.storage
        confirmations = storage.confirmations()
        rows = list(correction_rows(confirmations, p, values))
        for serial, changes in self.individual_drafts.items():
            rows.extend(correction_rows(confirmations, p, changes, records[serial].instrument))
        try:
            if rows:
                storage.save_confirmations(rows)
        except sqlite3.Error:
            logging.exception("Ошибка сохранения уточнений")
            self.status.setText("Не удалось сохранить изменения. Повторите попытку.")
            return
        self.party_draft.clear()
        self.individual_drafts.clear()
        self.window.recheck()
        self.refresh()

    def retry(self):
        self.collect()
        self.window.start_registry_checks()
        thread = self.window.lookup_thread
        if thread:
            self.equipment_panel.retry_btn.setEnabled(False)
            self.status.setText("Повторная проверка средств поверки…")
            thread.finished.connect(self.changed_equipment)

    def remove_protocol(self):
        self.window.protocols.remove(self.protocol)
        self.window.notice.clear()
        self.accept()
        self.window.recheck()


def laboratory_settings(parent, storage):
    settings = storage.laboratory()
    form = Form(parent, "Реквизиты лаборатории",
                "Указывайте постоянные сведения. Программа использует их, когда соответствующее поле в Excel пустое.",
                size=(665, 370), save="Сохранить настройки")
    for key, title, kind in DEFAULT_FIELDS:
        control = form.field(key, title, kind, settings.get(key))
        if key == "sign_cipher":
            control.setPlaceholderText("Например, ВЦЯ")
    form.validate = validate_defaults
    if form.exec():
        storage.save_laboratory({**settings, **form.values()}, getuser())


class ExpiryRules(QDialog):
    def __init__(self, parent, storage):
        super().__init__(parent)
        self.storage = storage
        self.setWindowTitle("Правила срока поверки")
        fit_window(self, 870, 440)
        box = QVBoxLayout(self)
        box.addWidget(label("Правила срока поверки", "heading"))
        box.addWidget(label("Добавьте правило по документам. Оно действует для указанного типа и дат поверки, "
                            "если межповерочный интервал в Excel совпадает и калибровка не использовалась."))
        self.view, self.model = table(("Тип СИ", "С даты", "По дату", "Лет", "Основание"), (120, 115, 115, 65))
        box.addWidget(self.view, 1)
        actions = QHBoxLayout()
        actions.addWidget(button("Добавить", lambda: self.edit({}), primary=True))
        self.change = button("Изменить", lambda: self.edit(selected(self.view)))
        self.remove = button("Удалить правило", self.remove_rule)
        actions.addWidget(self.change)
        actions.addWidget(self.remove)
        actions.addStretch()
        actions.addWidget(button("Закрыть", self.accept))
        box.addLayout(actions)
        self.view.selectionModel().currentChanged.connect(self.update_buttons)
        self.refresh()

    def update_buttons(self, *_):
        active = selected(self.view) is not None
        self.change.setEnabled(active)
        self.remove.setEnabled(active)

    def refresh(self):
        rules = self.storage.laboratory().get("expiry_rules", {}).values()
        self.model.update([(rule, (rule["type_number"], display_day(rule["from_date"]),
                                  display_day(rule["until_date"]), rule["years"], rule["document"])) for rule in rules])
        self.update_buttons()

    def edit(self, existing):
        if existing is None:
            return
        form = Form(self, "Подтверждённое правило срока",
                    "Программа ставит последний день перед годовщиной. Используйте правило только "
                    "если именно этот способ расчёта установлен для указанного типа.",
                    size=(650, 435), save="Сохранить правило")
        for key, title, kind in (
            ("type_number", "Регистрационный номер типа СИ", "text"),
            ("from_date", "Действует для поверок с", "date"),
            ("until_date", "Действует для поверок по (при наличии)", "date"),
            ("years", "Межповерочный интервал, полных лет", "text"),
            ("document", "Документ и пункт, устанавливающий правило", "text"),
        ):
            form.field(key, title, kind, existing.get(key))
        if existing:
            form.editor.controls["type_number"].setReadOnly(True)
        settings = self.storage.laboratory()
        rules = settings.setdefault("expiry_rules", {})

        def validate(values):
            rule = expiry_rule(values)
            if not existing and rule["type_number"] in rules:
                raise ValueError("Для этого типа уже есть правило. Выберите его и нажмите «Изменить».")

        form.validate = validate
        if form.exec():
            rule = expiry_rule(form.values())
            rules[rule["type_number"]] = rule
            self.storage.save_laboratory(settings, getuser())
            self.refresh()

    def remove_rule(self):
        rule = selected(self.view)
        if rule and message(self, "Удалить правило срока", "Сохранённое правило перестанет применяться.", ask=True):
            settings = self.storage.laboratory()
            settings["expiry_rules"].pop(rule["type_number"], None)
            self.storage.save_laboratory(settings, getuser())
            self.refresh()


class PreviewDialog(QDialog):
    def __init__(self, parent, records, *, type_titles=None):
        super().__init__(parent)
        self.type_titles = type_titles or {}
        title = "Результаты перед выгрузкой"
        self.setWindowTitle(title)
        fit_window(self, 830, 570)
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.addWidget(label(title, "heading"))
        ready = sum(not record.blockers for record in records)
        box.addWidget(label(f"Готово к выгрузке {ready} из {len(records)}. Выберите прибор для просмотра карточки."))
        self.type_label = label("")
        box.addWidget(self.type_label)
        self.view, self.model = table(("Заводской номер", "Модификация", "Дата поверки", "Результат"), (130, 170, 120))
        box.addWidget(self.view, 1)
        actions = QHBoxLayout()
        self.card_btn = button("Карточка прибора", self.open_card, primary=True)
        actions.addWidget(self.card_btn)
        actions.addStretch()
        actions.addWidget(button("Закрыть", self.accept))
        box.addLayout(actions)
        self.view.selectionModel().currentChanged.connect(self.select_record)
        self.view.doubleClicked.connect(self.open_card)
        self.model.update([(r, (r.instrument.serial, r.value("modification") or r.protocol.proposed_modification,
                                display_day(r.value("verification_date")), OUTCOMES[r.value("outcome") or ""] +
                                (" · требуется уточнение" if r.blockers else ""))) for r in records])
        if records:
            self.view.selectRow(0)
        self.select_record()

    def type_text(self, item):
        number = item.value("type_number")
        if not number:
            return ""
        return " · ".join(filter(None, ("Тип СИ", number, self.type_titles.get(number))))

    def select_record(self, *_):
        item = selected(self.view)
        self.card_btn.setEnabled(item is not None)
        title = self.type_text(item) if item else ""
        self.type_label.setText(title)
        self.type_label.setVisible(bool(title))

    def open_card(self, *_):
        item = selected(self.view)
        if not item:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Прибор {item.instrument.serial}")
        fit_window(dialog, 700, 590)
        box = QVBoxLayout(dialog)
        box.addWidget(label(f"Прибор {item.instrument.serial}", "heading"))
        if type_text := self.type_text(item):
            box.addWidget(label(type_text))
        method = label(f"Методика поверки · {item.value('method') or 'не указана в протоколе'}")
        method.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(method)
        if item.blockers:
            box.addWidget(label("Предварительные сведения. Запись требует уточнения перед выгрузкой."))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Сведения", "Значение"])
        tree.setColumnWidth(0, 245)
        try:
            for child in result_element(item):
                if child.tag != f"{{{NS}}}docTitle":
                    tree_items(child, tree)
        except ValueError:
            message(self, "Карточка прибора", "Сведения содержат недопустимые символы.", error=True)
            return
        tree.expandToDepth(1)
        box.addWidget(tree, 1)
        box.addWidget(button("Закрыть", dialog.accept))
        dialog.exec()


class ConnectionThread(QThread):
    checked = Signal(object)

    def run(self):
        self.checked.emit(check_connection())


class ConnectionDialog(QDialog):
    def __init__(self, parent, open_log):
        super().__init__(parent)
        self.setWindowTitle("Подключение к ФИФ ОЕИ")
        fit_window(self, 590, 320)
        self.worker = None
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.addWidget(label("Подключение к ФИФ ОЕИ", "heading"))
        self.status = label("Проверка использует текущие сетевые настройки Windows.")
        box.addWidget(self.status)
        self.view, self.model = table(("Реестр", "Состояние"), (250,))
        self.model.update([(name, (title, "Проверка ещё не выполнена")) for name, title in self.names().items()])
        box.addWidget(self.view, 1)
        actions = QHBoxLayout()
        self.run_btn = button("Проверить подключение", self.start, primary=True)
        actions.addWidget(self.run_btn)
        actions.addWidget(button("Открыть журнал", open_log))
        actions.addStretch()
        actions.addWidget(button("Закрыть", self.reject))
        box.addLayout(actions)

    @staticmethod
    def names():
        return {"MIT": "Типы приборов · MIT", "MIETA": "Эталоны · MIETA", "VRI": "Результаты поверок · VRI"}

    def start(self):
        self.run_btn.setEnabled(False)
        self.status.setText("Проверяем подключение…")
        self.worker = ConnectionThread(self)
        self.worker.checked.connect(self.show_results)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def show_results(self, report):
        self.model.update([(name, (title, report.get(name, "Проверка прервана")))
                           for name, title in self.names().items()])
        self.status.setText("Проверка завершена.")

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.run_btn.setEnabled(True)

    def reject(self):
        if self.worker is None:
            super().reject()
