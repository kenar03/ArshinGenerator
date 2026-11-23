from getpass import getuser

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

from ..laboratory import DEFAULT_FIELDS, expiry_rule, validate_defaults
from ..public_registry import check_connection
from .common import Form, button, fit_window, label, message, selected, table
from .protocols import display_day


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
