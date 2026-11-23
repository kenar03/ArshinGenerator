from getpass import getuser
import re

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QScrollArea, QTabWidget, QTreeWidget, QVBoxLayout, QWidget

from ..arshin import result_element
from ..confirmations import get_confirmation, individual_key, protocol_key, with_guards
from ..domain import OUTCOMES
from ..normalization import metrologist, serial_list
from ..public_registry import check_connection
from ..registry import iso_date
from ..validation import INDIVIDUAL_FIELDS, SHARED_FIELDS, modification_key, valid_modification
from .common import Fields, Form, button, fit_window, label, message, selected, table
from .dialogs import EquipmentReviewPanel, tree_items

PARTY_FIELDS = {"valid_date", "valid_date_absent", "sign_pass", "sign_mi"}
COMMON_FIELDS = (*SHARED_FIELDS, ("modification", "Подтверждённая модификация", "text"))


def readable(issue: str) -> str:
    issue = re.sub(r"[«»]?\b(?:Таблицы|Данные|Оборудование|Отчёт)[»]?\s*!\s*\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?",
                   "исходном протоколе", issue)
    issue = re.sub(r"\s*\(исходном протоколе\)", "", issue)
    return issue


def display_day(value) -> str:
    day = iso_date(value)
    return day.strftime("%d.%m.%Y") if day else ""


def save_corrections(storage, protocol, values, instrument=None):
    who = getuser()
    scope = "instrument" if instrument else "protocol"
    key = individual_key(protocol, instrument.serial) if instrument else protocol_key(protocol)
    prior = get_confirmation(storage.confirmations(), scope, protocol, instrument)
    fields = dict(prior[0]) if prior else {}
    values = dict(values)
    modification = values.pop("modification", "")
    fields.update(values)
    if values:
        storage.save_confirmation(scope, key, with_guards(protocol, fields, instrument),
                                  "Уточнено пользователем по документам поверки", who)
    if modification:
        storage.save_confirmation("modification", modification_key(protocol), {"modification": modification},
                                  "Модификация подтверждена пользователем", who)


class RepairDialog(QDialog):
    def __init__(self, window, protocol):
        super().__init__(window)
        self.window = window
        self.protocol = protocol
        self.setWindowTitle("Уточнение протокола")
        fit_window(self, 930, 635)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(label(protocol.number or protocol.path.name, "heading"))
        self.status = label("")
        layout.addWidget(self.status)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        actions = QHBoxLayout()
        remove = button("Убрать из списка", self.remove_protocol)
        remove.setAutoDefault(False)
        actions.addWidget(remove)
        actions.addStretch()
        close = button("Закрыть", self.accept)
        close.setDefault(True)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self):
        old = self.tabs.tabText(self.tabs.currentIndex())
        while self.tabs.count():
            item = self.tabs.widget(0)
            self.tabs.removeTab(0)
            item.deleteLater()
        p = self.protocol
        records = self.window.group[id(p)]
        good = sum(not r.blockers for r in records)
        self.status.setText(f"Приборов {len(records)}    ·    Готово к выгрузке {good}")
        common = set(records[0].missing_fields) if records else set()
        common.update(key for r in records for key in r.repair_fields & PARTY_FIELDS)
        equipment = records[0].equipment_issues if records else ()
        if equipment:
            panel = EquipmentReviewPanel(self, self.window.storage, p, self.window.lookups)
            if not panel.item_model.rows and p.equipment:
                panel.note.setText(" ".join(equipment))
            panel.changed.connect(self.saved)
            panel.retry.connect(self.retry)
            self.tabs.addTab(panel, "Средства поверки")
        shared = [issue for issue in records[0].shared_issues if issue not in equipment] if records else p.issues
        if p.error or shared or common:
            self.tabs.addTab(self.party_page(records, common, [p.error] if p.error else shared), "Недостающие сведения")
        individual = [r for r in records if r.issues and
                      (r.repair_fields - PARTY_FIELDS or not r.repair_fields)]
        # A recorded negative/contradictory measuring result can be present without an editable field.
        individual.extend(r for r in records if r not in individual and r.instrument.problem)
        if individual:
            self.tabs.addTab(self.results_page(individual), "Результаты приборов")
        if p.notices:
            page = QWidget()
            column = QVBoxLayout(page)
            for notice in p.notices:
                column.addWidget(label(readable(notice)))
            column.addStretch()
            self.tabs.addTab(page, "Предупреждения")
        if not self.tabs.count():
            page = QWidget()
            box = QVBoxLayout(page)
            box.addWidget(label("Протокол готов к выгрузке.", "heading"))
            if records:
                box.addWidget(button("Просмотреть результаты", lambda: PreviewDialog(self, records).exec()))
            box.addStretch()
            self.tabs.addTab(page, "Итог")
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == old:
                self.tabs.setCurrentIndex(index)
                break

    def party_page(self, records, common, issues):
        p = self.protocol
        editable = [(key, title, kind) for key, title, kind in COMMON_FIELDS if key in common]
        page = QWidget()
        box = QVBoxLayout(page)
        if issues:
            line = "\n".join(f"• {readable(issue)}" for issue in dict.fromkeys(issues))
            summary = QScrollArea()
            summary.setWidgetResizable(True)
            summary.setMaximumHeight(150 if editable else 390)
            summary.setWidget(label(line))
            box.addWidget(summary, 1)
        if not editable:
            box.addStretch()
            return page
        editor = Fields()
        current = records[0]
        previous = get_confirmation(self.window.storage.confirmations(), "protocol", p)
        previous = previous[0] if previous else {}
        for key, title, kind in editable:
            value = current.value(key)
            if key in {"sign_pass", "sign_mi"} and len(records) > 1:
                value = previous.get(key)
            control = editor.field(key, title, kind, value)
            if key == "modification" and p.proposed_modification:
                control.setPlaceholderText(f"В протоколе · {p.proposed_modification}")
        initial = editor.values()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(editor)
        box.addWidget(scroll, 2)

        def save():
            values = {key: value for key, value in editor.values().items() if value != initial[key]}
            if not values:
                return
            try:
                if values.get("sign_cipher") and not re.fullmatch(r"[А-Я]{1,3}", values["sign_cipher"]):
                    raise ValueError("Шифр знака должен содержать от одной до трёх заглавных русских букв.")
                if values.get("metrologist"):
                    metrologist(values["metrologist"])
                if values.get("valid_date") and not iso_date(values["valid_date"]):
                    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД.")
                if values.get("modification") and not valid_modification(p, values["modification"]):
                    raise ValueError("Уточните написание модификации по описанию типа.")
                if values.get("explicit_serials") and serial_list(values["explicit_serials"], explicit=True) != [
                    i.serial for i in p.instruments
                ]:
                    raise ValueError("Подтверждённый список должен совпасть с измеренными приборами.")
                save_corrections(self.window.storage, p, values)
            except ValueError as exc:
                message(self, "Уточнение сведений", str(exc), error=True)
                return
            self.saved()

        box.addWidget(button("Сохранить для партии", save, primary=True))
        return page

    def results_page(self, records):
        page = QWidget()
        box = QVBoxLayout(page)
        view, model = table(("Заводской номер", "Результат", "Что уточнить"), (135, 125))
        model.update([(r, (r.instrument.serial, OUTCOMES[r.value("outcome") or ""],
                           "; ".join(readable(issue) for issue in r.issues))) for r in records])
        box.addWidget(view, 1)
        notes = label("")
        notes.setMaximumHeight(105)
        box.addWidget(notes)
        edit = button("Уточнить прибор", lambda: self.edit_result(selected(view)), primary=True)
        box.addWidget(edit)

        def update(*_):
            item = selected(view)
            notes.setText("\n".join(readable(issue) for issue in item.issues) if item else "")
            edit.setEnabled(bool(item and any(key in item.repair_fields for key, _, _ in INDIVIDUAL_FIELDS)))

        view.selectionModel().currentChanged.connect(update)
        view.doubleClicked.connect(lambda *_: self.edit_result(selected(view)) if edit.isEnabled() else None)
        view.selectRow(0)
        update()
        return page

    def edit_result(self, result):
        if not result:
            return
        form = Form(self, f"Прибор {result.instrument.serial}", "Уточните сведения по документам поверки.",
                    size=(590, 445), save="Сохранить")
        keys = set(result.repair_fields)
        if "outcome" in keys:
            keys.add("reasons")
        for key, title, kind in INDIVIDUAL_FIELDS:
            if key in keys:
                form.field(key, title, kind, result.value(key))
        initial = form.values()

        def validate(values):
            if values.get("sticker") and not re.fullmatch(r"[0-9]{11}", values["sticker"]):
                raise ValueError("Введите фактический номер наклейки из 11 цифр.")
            if values.get("outcome") == "pass" and result.candidate == "fail":
                raise ValueError("Пригодность противоречит результатам измерений.")

        form.validate = validate
        if form.exec():
            values = {key: value for key, value in form.values().items() if value != initial[key]}
            if values:
                save_corrections(self.window.storage, self.protocol, values, result.instrument)
                self.saved()

    def saved(self):
        self.window.recheck()
        self.refresh()

    def retry(self):
        self.accept()
        self.window.start_registry_checks()

    def remove_protocol(self):
        self.window.protocols.remove(self.protocol)
        self.accept()
        self.window.recheck()


class PreviewDialog(QDialog):
    def __init__(self, parent, records):
        super().__init__(parent)
        self.setWindowTitle("Результаты перед выгрузкой")
        fit_window(self, 830, 570)
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        ready = sum(not r.blockers for r in records)
        box.addWidget(label("Результаты перед выгрузкой", "heading"))
        box.addWidget(label(f"Готово к выгрузке {ready} из {len(records)}. Выберите прибор для просмотра карточки."))
        self.view, self.model = table(("Заводской номер", "Модификация", "Дата поверки", "Результат"), (130, 170, 120))
        self.model.update([(r, (r.instrument.serial, r.value("modification") or r.protocol.proposed_modification,
                                display_day(r.value("verification_date")), OUTCOMES[r.value("outcome") or ""] +
                                (" · требуется уточнение" if r.blockers else ""))) for r in records])
        box.addWidget(self.view, 1)
        actions = QHBoxLayout()
        self.card_btn = button("Карточка прибора", self.open_card, primary=True)
        actions.addWidget(self.card_btn)
        actions.addStretch()
        actions.addWidget(button("Закрыть", self.accept))
        box.addLayout(actions)
        self.view.selectionModel().currentChanged.connect(
            lambda *_: self.card_btn.setEnabled(selected(self.view) is not None))
        self.view.doubleClicked.connect(self.open_card)
        self.card_btn.setEnabled(bool(records))
        self.view.selectRow(0)

    def open_card(self, *_):
        item = selected(self.view)
        if not item:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Прибор {item.instrument.serial}")
        fit_window(dialog, 700, 590)
        box = QVBoxLayout(dialog)
        box.addWidget(label(f"Прибор {item.instrument.serial}", "heading"))
        if item.blockers:
            box.addWidget(label("Предварительные сведения. Запись требует уточнения перед выгрузкой."))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Сведения", "Значение"])
        tree.setColumnWidth(0, 245)
        try:
            for child in result_element(item):
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
