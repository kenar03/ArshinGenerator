from getpass import getuser
import sqlite3

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..confirmations import get_confirmation, individual_key, protocol_key, with_guards
from ..domain import OUTCOMES
from ..laboratory import validate_defaults
from ..registry import iso_date
from ..validation import valid_sticker
from .common import Fields, button, fit_window, label, table
from .dialogs import EquipmentReviewPanel


def display_day(value):
    day = iso_date(value)
    return day.strftime("%d.%m.%Y") if day else "—"


EXTRA_FIELDS = (
    ("verification_type", "Вид поверки", "choice"),
    ("sign_cipher", "Шифр знака поверки", "text"),
    ("calibration", "Использованы результаты калибровки", "bool"),
    ("valid_date", "Дата окончания по документу", "date"),
    ("valid_date_absent", "Для типа предусмотрена только первичная поверка", "bool"),
    ("one_result", "По описанию типа одна запись на прибор с двумя ЧЭ", "bool"),
)


class ProtocolDialog(QDialog):
    def __init__(self, window, protocol=None):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Сведения о поверке")
        fit_window(self, 940, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        self.selector = QComboBox()
        for item in window.protocols:
            self.selector.addItem(item.number or item.path.name, item)
        if protocol is not None:
            self.selector.setCurrentIndex(next(i for i, item in enumerate(window.protocols) if item is protocol))
        layout.addWidget(self.selector)
        self.selector.setVisible(len(window.protocols) > 1)
        self.banner = label("")
        layout.addWidget(self.banner)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        layout.addWidget(self.scroll, 1)
        self.save_btn = button("Сохранить сведения", self.save, primary=True)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.save_btn)
        row.addWidget(button("Закрыть", self.accept))
        layout.addLayout(row)
        self.selector.currentIndexChanged.connect(self.refresh)
        window.checked.connect(self.refresh)
        self.refresh()

    def refresh(self):
        protocol = self.selector.currentData()
        records = self.window.group.get(id(protocol), [])
        self.protocol = protocol
        self.records = records
        self.editor = None
        self.equipment_panel = None
        self.banner.setText("")
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(10)
        if protocol:
            box.addWidget(label(protocol.number or protocol.path.name, "heading"))
            if records:
                self.show_records(box, records)
            registry_stage = any(item is protocol for item in self.window.registry_protocols)
            self.show_issues(box, protocol, records, registry_stage)
            if records:
                self.add_missing_fields(box, records)
            if protocol.equipment:
                self.equipment_panel = EquipmentReviewPanel(self, self.window.storage, protocol,
                                                           self.window.lookups if registry_stage else {})
                self.equipment_panel.changed.connect(self.window.recheck)
                self.equipment_panel.retry.connect(self.window.start_registry_checks)
                self.equipment_panel.setEnabled(not self.window.busy)
                box.addWidget(self.equipment_panel)
            number = protocol.fields.get("type_number")
            if number and self.window.type_issues.get(number.value, "").startswith("Не удалось"):
                retry = button("Повторить поиск типа СИ", self.window.start_registry_checks)
                retry.setEnabled(not self.window.busy)
                box.addWidget(retry)
        box.addStretch()
        old = self.scroll.takeWidget()
        self.scroll.setWidget(page)
        if old:
            old.deleteLater()
        self.save_btn.setVisible(self.editor is not None)
        self.save_btn.setEnabled(self.editor is not None and not self.window.busy)

    def show_records(self, box, records):
        first = records[0]
        number = first.value("type_number")
        name = self.window.type_titles.get(number)
        type_text = " · ".join(filter(None, (str(number or ""), name)))
        summary = [
            f"Приборов {len(records)} · готово {sum(not r.blockers for r in records)}",
            f"Тип СИ · {type_text or 'не указан'}",
            f"Модификация · {first.value('modification') or 'не определена'}",
            f"Дата поверки · {display_day(first.value('verification_date'))}",
            f"Методика · {first.value('method') or 'не указана'}",
        ]
        box.addWidget(label("\n".join(summary)))
        view, model = table(("Заводской номер", "Результат", "Действительно до"), (145, 210))
        model.update([(r, (r.instrument.serial, OUTCOMES[r.value("outcome") or ""],
                          display_day(r.value("valid_date")))) for r in records])
        view.setFixedHeight(view.horizontalHeader().sizeHint().height() + min(6, len(records)) * 32 + 6)
        box.addWidget(view)

    def show_issues(self, box, protocol, records, registry_stage):
        equipment = {issue for r in records for issue in r.equipment_issues if " · " in issue} if registry_stage else set()
        issues = dict.fromkeys(issue for r in records for issue in r.shared_issues if issue not in equipment)
        individual = {}
        for record in records:
            for issue in record.issues:
                individual.setdefault(issue, []).append(record.instrument.serial)
        for issue, serials in individual.items():
            if issue not in issues:
                listed = ", ".join(serials[:5])
                more = f" и ещё {len(serials) - 5}" if len(serials) > 5 else ""
                issues[f"{listed}{more} · {issue}" if listed else issue] = None
        if not records:
            issues.update(dict.fromkeys([protocol.error] if protocol.error else
                                        protocol.issues or [protocol.serial_issue or "Нет отдельных СИ."]))
        if issues:
            box.addWidget(label("Что нужно исправить", "heading"))
            items = [issue.removeprefix("Подтверждение: ") for issue in issues]
            box.addWidget(label("\n".join(f"• {item[:1].upper()}{item[1:]}" for item in items)))
        notices = dict.fromkeys([*protocol.notices, *(warning for r in records for warning in r.warnings)])
        for notice in notices:
            box.addWidget(label(notice))

    def add_missing_fields(self, box, records):
        first = records[0]
        missing = set(first.missing_fields)
        missing.update(key for r in records for key in r.repair_fields if key in {"valid_date", "valid_date_absent"})
        kind = self.protocol.fields.get("verification_type")
        if kind and kind.raw.strip():
            missing.discard("verification_type")
        if first.value("verification_type") not in {"1", None, ""}:
            missing.discard("valid_date_absent")
        date = self.protocol.fields.get("valid_date")
        if date and date.value:
            missing.difference_update(("valid_date", "valid_date_absent"))
        fields = [(key, title, kind) for key, title, kind in EXTRA_FIELDS if key in missing]
        number = self.protocol.fields.get("sticker")
        sticker = (str(number.value).strip() if number and number.value and len(records) > 1
                   and valid_sticker(number.value) else "")
        assignable = [record for record in records if record.value("outcome") == "pass"]
        assign = sticker and assignable and not any(r.value("sticker") == sticker for r in assignable)
        if not fields and not assign:
            return
        box.addWidget(label("Недостающие сведения", "heading"))
        self.editor = Fields()
        for key, title, kind in fields:
            self.editor.field(key, title, kind, first.value(key))
        if assign:
            options = [("Выберите прибор", ""), *((r.instrument.serial, r.instrument.serial) for r in assignable)]
            self.editor.field("sticker_serial", f"Прибор с наклейкой {sticker}", choices=options)
        self.initial = self.editor.values()
        box.addWidget(self.editor)

    def save(self):
        if not self.editor or self.window.busy:
            return
        values = {key: value for key, value in self.editor.values().items() if value != self.initial[key]}
        serial = values.pop("sticker_serial", "")
        if not values and not serial:
            return
        try:
            validate_defaults(values)
            end = iso_date(values.get("valid_date"))
            start = iso_date(self.records[0].value("verification_date"))
            if values.get("valid_date") and not end:
                raise ValueError("Введите дату окончания в формате ДД.ММ.ГГГГ.")
            if end and start and end < start:
                raise ValueError("Дата окончания должна быть не раньше даты поверки.")
            confirmations = self.window.storage.confirmations()
            rows = []
            if values:
                previous = get_confirmation(confirmations, "protocol", self.protocol)
                prior = previous[0] if previous else {}
                rows.append(("protocol", protocol_key(self.protocol), with_guards(self.protocol, {**prior, **values}),
                             "Реквизиты поверки", getuser()))
            if serial:
                record = next(record for record in self.records if record.instrument.serial == serial)
                previous = get_confirmation(confirmations, "instrument", self.protocol, record.instrument)
                prior = previous[0] if previous else {}
                data = {**prior, "sticker": self.protocol.fields["sticker"].value}
                rows.append(("instrument", individual_key(self.protocol, serial),
                             with_guards(self.protocol, data, record.instrument), "Наклейка из протокола", getuser()))
            self.window.storage.save_confirmations(rows)
        except (ValueError, sqlite3.Error) as exc:
            self.banner.setText(str(exc) if isinstance(exc, ValueError) else "Не удалось сохранить сведения.")
            return
        self.window.refresh_after_edit()
