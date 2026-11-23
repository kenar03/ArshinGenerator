from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..domain import OUTCOMES
from ..registry import iso_date
from .common import button, fit_window, label, table
from .dialogs import EquipmentReviewPanel


def display_day(value):
    day = iso_date(value)
    return day.strftime("%d.%m.%Y") if day else "—"


def examples(serials):
    names = ", ".join(serial for serial in serials[:4] if serial)
    rest = len(serials) - 4
    return names + (f" и ещё {rest}" if rest > 0 else "") if names else ""


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
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        layout.addWidget(self.scroll, 1)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Закрыть", self.accept))
        layout.addLayout(row)
        self.selector.currentIndexChanged.connect(self.refresh)
        window.checked.connect(self.refresh)
        self.refresh()

    def refresh(self):
        protocol = self.selector.currentData()
        self.records = self.window.group.get(id(protocol), [])
        self.equipment_panel = None
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(10)
        if protocol:
            box.addWidget(label(protocol.number or protocol.path.name, "heading"))
            if self.window.busy:
                box.addWidget(label("Проверяем сведения…"))
            elif self.window.complete(protocol, self.records):
                box.addWidget(label("Готов к выгрузке"))
            if not protocol.error:
                self.show_records(box, protocol)
            in_registry_stage = any(item is protocol for item in self.window.registry_protocols)
            self.show_issues(box, protocol, in_registry_stage)
            if protocol.equipment:
                self.equipment_panel = EquipmentReviewPanel(self, self.window.storage, protocol,
                    self.window.lookups if in_registry_stage else {}, checked_inputs=in_registry_stage)
                self.equipment_panel.changed.connect(self.window.recheck)
                self.equipment_panel.setEnabled(not self.window.busy)
                box.addWidget(self.equipment_panel)
            if in_registry_stage:
                requests, number = self.window.registry_keys(protocol)
                retry = number in self.window.retryable_types or any(
                    self.window.lookups[key].state in {"unavailable", "response"}
                    for key in requests if key in self.window.lookups
                )
                if retry and not self.window.busy:
                    box.addWidget(button("Повторить проверку", lambda: self.window.retry_registry(protocol)))
        box.addStretch()
        old = self.scroll.takeWidget()
        self.scroll.setWidget(page)
        if old:
            old.deleteLater()

    def show_records(self, box, protocol):
        def value(key):
            if self.records:
                return self.records[0].value(key)
            return protocol.fields[key].value if key in protocol.fields else None

        number = value("type_number")
        name = self.window.type_titles.get(number)
        type_text = " · ".join(filter(None, (str(number or ""), name)))
        model = value("modification") or value("model")
        lines = [
            f"Приборов {protocol.declared or len(self.records)}",
            f"Тип СИ · {type_text or 'не указан'}",
            f"Модель и исполнение · {model or 'не указаны'}",
            f"Дата поверки · {display_day(value('verification_date'))}",
        ]
        if value("valid_date") and any(record.value("outcome") != "fail" for record in self.records):
            lines.append(f"Действительно до · {display_day(value('valid_date'))}")
        if self.records and all(record.instrument.outcome for record in self.records):
            outcome = "fail" if any(record.instrument.outcome == "fail" for record in self.records) else "pass"
            lines.append(f"Общий результат · {OUTCOMES[outcome]}")
        box.addWidget(label("\n".join(lines)))
        if not self.records:
            return
        view, model = table(("Заводской номер", "Результат"), (195,))
        model.update([(r, (r.instrument.serial, OUTCOMES.get(r.value("outcome"), OUTCOMES[""]))) for r in self.records])
        view.setFixedHeight(view.horizontalHeader().sizeHint().height() + min(6, len(self.records)) * 32 + 6)
        box.addWidget(view)

    def show_issues(self, box, protocol, in_registry_stage):
        registry = dict.fromkeys(issue for record in self.records for issue in record.registry_issues)
        equipment = {issue for record in self.records for issue in record.equipment_issues}
        issues = dict.fromkeys(issue for record in self.records for issue in record.shared_issues if issue not in registry)
        group = {}
        incomplete = []
        for record in self.records:
            if record.incomplete_measurements:
                incomplete.append(record.instrument.serial)
            for issue in record.issues:
                if record.incomplete_measurements and issue.startswith("Не завершены измерения."):
                    continue
                title, _, hint = issue.partition(" Проверьте ")
                if title not in group:
                    group[title] = (hint, [])
                group[title][1].append(record.instrument.serial)
        if incomplete:
            serials = examples(incomplete)
            hint = f" ({serials})" if serials else ""
            issues[f"Приборов с незавершёнными измерениями: {len(incomplete)}{hint}. {protocol.measurement_hint}"] = None
        for title, (location, serials) in group.items():
            names = examples(serials)
            detail = f" · {names}" if names else ""
            hint = f" Проверьте {location}" if location else ""
            issues[f"{title.rstrip('.')}{detail}.{hint}"] = None
        if not self.records:
            issues.update(dict.fromkeys(self.window.source_issues.get(id(protocol), ())))
        registry_only = [issue for issue in registry if issue not in equipment or " · " not in issue]
        if issues or registry:
            box.addWidget(label("Что нужно исправить", "heading"))
        if issues:
            box.addWidget(label("\n".join(f"• {issue}" for issue in issues)))
        if registry and in_registry_stage:
            box.addWidget(label("ФИФ ОЕИ", "heading"))
        if registry_only:
            box.addWidget(label("\n".join(f"• {issue}" for issue in registry_only)))
        for notice in protocol.notices:
            box.addWidget(label(notice))
