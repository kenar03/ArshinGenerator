import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..registry import iso_date
from .common import button, display, fit_window, label
from .dialogs import EquipmentReviewPanel

SERIES_NUMBER = re.compile(r"([0-9]+)([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9]*)?")
SERIAL_PREVIEW_COUNT = 3


def display_day(value):
    day = iso_date(value)
    return day.strftime("%d.%m.%Y") if day else "—"


def examples(serials, limit=4):
    names = ", ".join(serial for serial in serials[:limit] if serial)
    rest = len(serials) - limit
    return names + (f" и ещё {rest}" if rest > 0 else "") if names else ""


def serial_text(instrument):
    number = instrument.serial
    # The fallback reader can store the entire batch in raw when measurement rows are missing.
    raw = instrument.raw if instrument.elements else ""
    if raw and raw != number:
        return f"{raw} (в XML: {number})" if number else raw
    return number


def serial_summary(serials):
    if len(serials) <= SERIAL_PREVIEW_COUNT:
        return ", ".join(serials)
    matches = [SERIES_NUMBER.fullmatch(number) for number in serials]
    first = matches[0]
    if first:
        digits, suffix = first.groups()
        start = int(digits)
        if all(match and len(match[1]) == len(digits) and match[2] == suffix and int(match[1]) == start + index
               for index, match in enumerate(matches)):
            count = len(serials)
            form = "приборов"
            if not 11 <= count % 100 <= 14:
                form = {1: "прибор", 2: "прибора", 3: "прибора", 4: "прибора"}.get(count % 10, form)
            return f"{serials[0]}–{serials[-1]} ({count} {form})"
    return examples(serials, SERIAL_PREVIEW_COUNT)


def section_label(text):
    widget = label(text)
    widget.setStyleSheet("font-weight: 600;")
    return widget


class ProtocolDialog(QDialog):
    def __init__(self, window, protocol=None):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Сведения о поверке")
        fit_window(self, 835, 600)
        self.protocol = None
        self.serial_toggle = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        self.selector = QComboBox()
        for item in window.protocols:
            self.selector.addItem(item.number or item.path.name, item)
        if protocol is not None:
            self.selector.setCurrentIndex(next(i for i, item in enumerate(window.protocols) if item is protocol))
        layout.addWidget(self.selector)
        self.selector.setVisible(len(window.protocols) > 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        expanded = self.protocol is protocol and self.serial_toggle is not None and self.serial_toggle.isChecked()
        self.protocol = protocol
        self.serial_toggle = None
        self.records = self.window.group.get(id(protocol), [])
        self.equipment_panel = None
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 6, 10, 8)
        box.setSpacing(8)
        if protocol:
            box.addWidget(section_label(f"Протокол {protocol.number or protocol.path.name}"))
            if self.window.busy:
                box.addWidget(label("Проверяем сведения…"))
            elif self.window.complete(protocol, self.records):
                box.addWidget(label("Готов к выгрузке"))
            if not protocol.error:
                self.show_records(box, protocol, expanded)
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

    def show_records(self, box, protocol, expanded):
        def value(key):
            if self.records:
                return self.records[0].value(key)
            return protocol.fields[key].value if key in protocol.fields else None

        number = value("type_number")
        name = self.window.type_titles.get(number)
        type_text = " · ".join(filter(None, (str(number or ""), name)))
        marking = " / ".join(dict.fromkeys(str(item) for item in (value("model"), value("modification")) if item))
        passed = sum(r.value("outcome") == "pass" for r in self.records)
        failed = [serial_text(r.instrument) for r in self.records if r.value("outcome") == "fail"]
        details = QFormLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setHorizontalSpacing(14)
        details.setVerticalSpacing(3)
        details.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        details.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for title, text in (
            ("Тип СИ", type_text),
            ("Модель и модификация", marking),
            ("Владелец СИ", value("owner")),
            ("Поверитель", value("metrologist")),
            ("Методика поверки", value("method")),
            ("Дата поверки", display_day(value("verification_date"))),
            ("Действительна до", display_day(value("valid_date") if passed else None)),
        ):
            caption = label(title)
            caption.setWordWrap(False)
            details.addRow(caption, label(display(text)))
        box.addLayout(details)

        total = max(protocol.declared or 0, len(self.records))
        problems = total - len(self.records) + sum(bool(r.blockers) for r in self.records)
        totals = f"Приборов: {total} · Пригодны: {passed} · Непригодны: {len(failed)}"
        if problems:
            totals += f" · Проблемных: {problems}"
        box.addWidget(section_label(totals))

        serials = [text for r in self.records if (text := serial_text(r.instrument))]
        self.show_serials(box, serials, expanded)
        if failed:
            box.addWidget(section_label("Непригодные приборы"))
            box.addWidget(label(", ".join(filter(None, failed)) or "Заводские номера не прочитаны"))

    def show_serials(self, box, serials, expanded):
        box.addWidget(section_label("Заводские номера"))
        row = QHBoxLayout()
        summary = label(serial_summary(serials) or "Номера не прочитаны")
        summary.ensurePolished()
        summary.setMaximumWidth(summary.fontMetrics().horizontalAdvance(summary.text()) + 2)
        row.addWidget(summary, 1)
        box.addLayout(row)
        row.addStretch()
        if len(serials) <= SERIAL_PREVIEW_COUNT:
            return
        toggle = button("Показать все")
        toggle.setStyleSheet("padding: 2px 8px;")
        toggle.setCheckable(True)
        row.insertWidget(1, toggle, alignment=Qt.AlignmentFlag.AlignTop)
        full = label(", ".join(serials))
        full.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        full.hide()
        box.addWidget(full)

        def show_full(visible):
            full.setVisible(visible)
            toggle.setText("Свернуть" if visible else "Показать все")

        toggle.toggled.connect(show_full)
        toggle.setChecked(expanded)
        self.serial_toggle = toggle

    def show_issues(self, box, protocol, in_registry_stage):
        registry = dict.fromkeys(issue for record in self.records for issue in record.registry_issues)
        equipment = {issue for record in self.records for issue in record.equipment_issues}
        issues = dict.fromkeys(issue for record in self.records for issue in record.shared_issues if issue not in registry)
        group = {}
        incomplete = []
        for record in self.records:
            if record.incomplete_measurements:
                incomplete.append(serial_text(record.instrument))
            for issue in record.issues:
                if record.incomplete_measurements and issue.startswith("Не завершены измерения."):
                    continue
                title, _, hint = issue.partition(" Проверьте ")
                if title not in group:
                    group[title] = (hint, [])
                group[title][1].append(serial_text(record.instrument))
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
            box.addWidget(section_label("Что нужно исправить"))
        if issues:
            box.addWidget(label("\n".join(f"• {issue}" for issue in issues)))
        if registry and in_registry_stage:
            box.addWidget(section_label("ФИФ ОЕИ"))
        if registry_only:
            box.addWidget(label("\n".join(f"• {issue}" for issue in registry_only)))
        for notice in protocol.notices:
            box.addWidget(label(notice))
