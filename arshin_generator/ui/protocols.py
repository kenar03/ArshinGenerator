import re

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..registry import iso_date
from .common import apply_dialog_icon, button, display, fit_window, label, message
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
        apply_dialog_icon(self)
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
        self.open_excel_btn = button("Открыть в Excel", self.open_excel)
        row.addWidget(self.open_excel_btn)
        row.addWidget(button("Закрыть", self.accept))
        layout.addLayout(row)
        self.selector.currentIndexChanged.connect(self.refresh)
        window.checked.connect(self.refresh)
        self.refresh()

    def open_excel(self):
        if self.protocol is None:
            return
        path = self.protocol.path
        try:
            with path.open("rb"):
                pass
        except OSError:
            pass
        else:
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                return
        message(self, "Исходный протокол", "Не удалось открыть исходный протокол. Проверьте, существует ли файл.",
                error=True)

    def refresh(self):
        protocol = self.selector.currentData()
        expanded = self.protocol is protocol and self.serial_toggle is not None and self.serial_toggle.isChecked()
        self.protocol = protocol
        self.open_excel_btn.setEnabled(protocol is not None)
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
            self.show_issues(box, protocol)
            if protocol.equipment:
                self.equipment_panel = EquipmentReviewPanel(self, self.window.storage, protocol,
                    self.window.lookups if in_registry_stage else {}, checked_inputs=in_registry_stage)
                self.equipment_panel.changed.connect(self.window.recheck)
                self.equipment_panel.setEnabled(not self.window.busy)
                box.addWidget(self.equipment_panel)
            if in_registry_stage or protocol in self.window.type_only_protocols:
                requests, number = self.window.registry_keys(protocol)
                retry = number in self.window.retryable_types or (in_registry_stage and any(
                    self.window.lookups[key].retryable for key in requests if key in self.window.lookups
                ))
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

        passed = sum(r.value("outcome") == "pass" for r in self.records)
        failed = [serial_text(r.instrument) for r in self.records if r.value("outcome") == "fail"]
        details = QFormLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setHorizontalSpacing(14)
        details.setVerticalSpacing(3)
        details.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        details.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        rows = [("Тип протокола", protocol.reader_name)]
        for field in protocol.preview_fields:
            text = value(field.key)
            if field.optional and text in (None, ""):
                continue
            if field.key == "type_number" and text and (name := self.window.type_titles.get(text)):
                text = f"{text} · {name}"
            rows.append((field.title, text))
        rows.extend((
            ("Владелец СИ", value("owner")),
            ("Поверитель", value("metrologist")),
            ("Методика поверки", value("method")),
            ("Дата поверки", display_day(value("verification_date"))),
            ("Действительна до", display_day(value("valid_date") if passed else None)),
        ))
        for title, text in rows:
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

    def show_issues(self, box, protocol):
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
                group.setdefault(title, {}).setdefault(hint, []).append(serial_text(record.instrument))
        if incomplete:
            serials = examples(incomplete)
            hint = f" ({serials})" if serials else ""
            issues[f"Приборов с незавершёнными измерениями: {len(incomplete)}{hint}. {protocol.measurement_hint}"] = None
        for title, locations in group.items():
            serials = [serial for names in locations.values() for serial in names]
            names = examples(serials)
            detail = f" · {names}" if names else ""
            hints = []
            for location, affected in locations.items():
                if not location:
                    continue
                affected_names = examples(affected) if len(locations) > 1 else ""
                prefix = f"Для № {affected_names} проверьте" if affected_names else "Проверьте"
                hints.append(f"{prefix} {location.rstrip('.')}.")
            issues[" ".join((f"{title.rstrip('.')}{detail}.", *hints))] = None
        if not self.records:
            issues.update(dict.fromkeys(self.window.source_issues.get(id(protocol), ())))
        registry_only = [issue for issue in registry if issue not in equipment]
        if issues or registry:
            box.addWidget(section_label("Что нужно исправить"))
        if issues:
            box.addWidget(label("\n".join(f"• {issue}" for issue in issues)))
        if registry_only:
            box.addWidget(section_label("ФИФ ОЕИ"))
            box.addWidget(label("\n".join(f"• {issue}" for issue in registry_only)))
        if protocol.notices:
            box.addWidget(section_label("Информационные замечания"))
            box.addWidget(label("\n".join(dict.fromkeys(protocol.notices))))
