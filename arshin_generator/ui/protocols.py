import re
import sqlite3
from getpass import getuser

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from ..arshin import result_values
from ..confirmations import YEAR_SCOPE, individual_key, manufacture_year, protocol_key
from ..domain import READY, RECHECK, UNVERIFIED
from ..registry import CATEGORIES, iso_date
from .common import apply_dialog_icon, button, display, fit_window, label, message, table
from .dialogs import TAGS, field_text

SERIES_NUMBER = re.compile(r"([0-9]+)([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9]*)?")
SERIAL_PREVIEW_COUNT = 3


def display_day(value):
    day = iso_date(value)
    return day.strftime("%d.%m.%Y") if day else value


def examples(serials, limit=4):
    names = ", ".join(serial for serial in serials[:limit] if serial)
    rest = len(serials) - limit
    return names + (f" и ещё {rest}" if rest > 0 else "") if names else ""


def serial_text(instrument):
    return instrument.serial or (instrument.raw if instrument.elements else "")


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


def show_fields(box, rows):
    rows = [(title, value) for title, value in rows if value not in (None, "")]
    if not rows:
        return
    details = QFormLayout()
    details.setHorizontalSpacing(14)
    details.setVerticalSpacing(3)
    details.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    details.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    for title, value in rows:
        caption = label(title)
        caption.setWordWrap(False)
        widget = label(display(value))
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details.addRow(caption, widget)
    box.addLayout(details)


def compact_table(box, headers, rows):
    view, model = table(headers, ())
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setWordWrap(True)
    header = view.horizontalHeader()
    header.setObjectName("previewHeader")
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    view.verticalHeader().setMinimumSectionSize(view.fontMetrics().height() + 10)

    def adjust_table():
        minimums = [header.sectionSizeHint(column) for column in range(len(headers))]
        share, extra = divmod(max(0, view.viewport().width() - sum(minimums)), len(headers))
        for column, width in enumerate(minimums):
            view.setColumnWidth(column, width + share + (column < extra))
        view.resizeRowsToContents()
        view.setFixedHeight(header.sizeHint().height() + view.verticalHeader().length() + 2 * view.frameWidth())

    timer = QTimer(view)
    timer.setSingleShot(True)
    timer.timeout.connect(adjust_table)
    header.geometriesChanged.connect(timer.start)
    model.modelReset.connect(adjust_table)
    model.update(rows)
    box.addWidget(view)
    return model


class ProtocolDialog(QDialog):
    def __init__(self, window, protocol=None):
        super().__init__(window)
        apply_dialog_icon(self)
        self.window = window
        self.setWindowTitle("Сведения о поверке")
        self.setMinimumWidth(min(510, self.screen().availableGeometry().width() - 24))
        fit_window(self, 835, 600)
        self.protocol = None
        self.serial_toggle = None
        self.year_selector = self.year_input = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        self.selector = QComboBox()
        self.selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for item in window.protocols:
            self.selector.addItem(item.number or item.path.name, item.path)
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
        path = self.selector.currentData()
        protocol = next((p for p in self.window.protocols if p.path == path), None)
        same = self.protocol is protocol
        same_path = protocol is not None and self.protocol is not None and self.protocol.path == protocol.path
        expanded = same_path and self.serial_toggle is not None and self.serial_toggle.isChecked()
        target = self.year_selector.currentData() if same_path and self.year_selector else None
        draft = self.year_input.text() if same and self.year_input and self.year_input.isModified() else None
        scroll_position = self.scroll.verticalScrollBar().value() if same_path else 0
        self.protocol = protocol
        self.open_excel_btn.setEnabled(protocol is not None)
        self.serial_toggle = None
        self.year_selector = self.year_input = None
        self.records = self.window.group.get(id(protocol), [])
        self.equipment_models = {}
        self.instrument_model = None
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 6, 10, 8)
        box.setSpacing(8)
        if protocol:
            self.selector.setItemText(self.selector.currentIndex(), protocol.number or protocol.path.name)
            heading = f"Протокол {protocol.number or protocol.path.name}"
            if protocol.reader_name:
                heading += f" · {protocol.reader_name}"
            box.addWidget(section_label(heading))
            state = self.window.state(protocol, self.records)
            if self.window.busy:
                box.addWidget(label("Проверяем сведения…"))
            elif state == READY:
                box.addWidget(label("Готов к выгрузке"))
            elif state in (RECHECK, UNVERIFIED):
                box.addWidget(label(state))
                box.addWidget(label("Проверьте связь с ФИФ ОЕИ и нажмите «Перепроверить»." if state == UNVERIFIED
                                    else "Сведения ФИФ ОЕИ будут проверены перед выгрузкой."))
            self.show_issues(box, protocol)
            if not protocol.error:
                values = [result_values(record) for record in self.records]
                self.show_instruments(box, protocol, values, expanded, target, draft)
                if values:
                    self.show_verification(box, values)
                self.show_equipment(box, protocol)
                if values:
                    self.show_other(box, values[0])
        box.addStretch()
        old = self.scroll.takeWidget()
        self.scroll.setWidget(page)
        if old:
            old.deleteLater()
        if scroll_position:
            QTimer.singleShot(0, page, lambda: self.scroll.verticalScrollBar().setValue(scroll_position))

    def show_instruments(self, box, protocol, values, expanded, target, draft):
        box.addWidget(section_label("Сведения о СИ"))
        first = values[0] if values else {}
        number = first.get("mitypeNumber")
        if not values and "type_number" in protocol.fields:
            number = protocol.fields["type_number"].value
        if number and (name := self.window.type_titles.get(number)):
            number = f"{number} · {name}"
        show_fields(box, ((TAGS["mitypeNumber"], number), (TAGS["modification"], first.get("modification"))))

        passed = sum("signPass" in value for value in values)
        failed = sum("reasons" in value for value in values)
        total = max(protocol.declared or 0, len(self.records))
        problems = total - len(self.records) + sum(bool(r.data_issues) for r in self.records)
        totals = f"Приборов: {total} · Пригодны: {passed} · Непригодны: {failed}"
        if problems:
            totals += f" · Проблемных: {problems}"
        box.addWidget(section_label(totals))
        if not values:
            return

        sticker_column = len(values) > 1 and any("stickerNum" in value for value in values)
        headers = (TAGS["manufactureNum"], TAGS["manufactureYear"], "Результат поверки")
        if sticker_column:
            headers += (TAGS["stickerNum"],)
        rows = []
        for record, value in zip(self.records, values):
            outcome = TAGS["applicable"] if "signPass" in value else TAGS["inapplicable"] if "reasons" in value else ""
            row = (value["manufactureNum"], value.get("manufactureYear"), outcome)
            if sticker_column:
                row += (value.get("stickerNum"),)
            rows.append((record, row))

        if len(rows) > SERIAL_PREVIEW_COUNT:
            row = QHBoxLayout()
            row.addWidget(label(serial_summary([value["manufactureNum"] for value in values])), 1)
            toggle = button("Показать все")
            toggle.setStyleSheet("padding: 2px 8px;")
            toggle.setCheckable(True)
            toggle.setChecked(expanded)
            if expanded:
                toggle.setText("Свернуть")
            row.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignTop)
            box.addLayout(row)
            self.serial_toggle = toggle

        model = compact_table(box, headers, rows if expanded else rows[:SERIAL_PREVIEW_COUNT])
        self.instrument_model = model
        if self.serial_toggle:
            def show_all(visible):
                model.update(rows if visible else rows[:SERIAL_PREVIEW_COUNT])
                toggle.setText("Свернуть" if visible else "Показать все")

            toggle.toggled.connect(show_all)
        self.show_year_editor(box, protocol, target, draft)

    def show_verification(self, box, values):
        box.addWidget(section_label("Сведения о поверке"))
        fields = values[0]
        show_fields(box, ((TAGS[tag], display_day(fields[tag]) if tag == "vrfDate" else field_text(tag, fields[tag]))
                          for tag in ("signCipher", "miOwner", "vrfDate", "docTitle", "type", "calibration", "metrologist")
                          if tag in fields))

        if applicable := next((value for value in values if "signPass" in value), None):
            box.addWidget(section_label(TAGS["applicable"]))
            show_fields(box, ((TAGS["validDate"], display_day(applicable.get("validDate"))),
                              (TAGS["stickerNum"], applicable.get("stickerNum") if len(values) == 1 else None),
                              (TAGS["signPass"], applicable.get("signPass")), (TAGS["signMi"], applicable.get("signMi"))))
        if inapplicable := [value for value in values if "reasons" in value]:
            box.addWidget(section_label(TAGS["inapplicable"]))
            show_fields(box, ((TAGS["reasons"], value["reasons"]) for value in inapplicable))

    def show_equipment(self, box, protocol):
        if not protocol.equipment:
            return
        resolutions = self.records[0].equipment_resolutions if self.records else {}
        checked = sum(bool(resolutions.get(item)) for item in protocol.equipment)
        summary = (f"Средства поверки: проверено {checked} из {len(protocol.equipment)}" if
                   resolutions or protocol in self.window.registry_protocols else "Средства поверки")
        box.addWidget(section_label(summary))
        grouped = {}
        for item in protocol.equipment:
            if entries := resolutions.get(item):
                for entry in entries:
                    number = entry.type_number if entry.category == "mis" else entry.identifier
                    inventory = entry.registry_serial if entry.category == "mis" and entry.identity_kind == "inventoryNum" else ""
                    row = (entry.name or item.name, "" if inventory else entry.registry_serial, number, inventory, "Проверено")
                    grouped.setdefault(entry.category, []).append((entry, row))
            else:
                status = "Не подтверждено" if item in resolutions or not item.category else "Не проверено"
                number = item.registration if item.category == "mieta" else item.type_number
                row = (item.name or item.description, item.serial, number, "", status)
                grouped.setdefault(item.category, []).append((item, row))
        for category in ("npe", "uve", "mieta", "mis", ""):
            if not (rows := grouped.get(category)):
                continue
            box.addWidget(section_label(CATEGORIES.get(category, "Категория не подтверждена")))
            number_label = {"mieta": "№ СИ по перечню", "mis": "Регистрационный\n№ типа СИ"}.get(category, "Номер эталона")
            headers = ("Наименование", "Заводской\nномер", number_label)
            if any(row[3] for _, row in rows):
                headers += ("Буквенно-\nцифровое\nобозначение",)
            else:
                rows = [(item, row[:3] + row[4:]) for item, row in rows]
            self.equipment_models[category] = compact_table(box, headers + ("Состояние",), rows)

    def show_other(self, box, values):
        condition_tags = ("temperature", "pressure", "hymidity", "other")
        other_tags = ("structure", "characteristics", "additional_info")
        has_conditions = any(values.get(tag) for tag in condition_tags)
        if not has_conditions and not any(tag in values for tag in other_tags):
            return
        box.addWidget(section_label("Прочие сведения"))
        if has_conditions:
            box.addWidget(label(TAGS["conditions"]))
            show_fields(box, ((TAGS[tag], values.get(tag)) for tag in condition_tags))
        show_fields(box, ((TAGS["structure"], values.get("structure")),
                          (TAGS["brief_procedure"], True if "characteristics" in values else None),
                          (TAGS["characteristics"], values.get("characteristics")),
                          (TAGS["additional_info"], values.get("additional_info"))))

    def show_year_editor(self, box, protocol, target, draft):
        if not self.records or not protocol.number or not all(
            protocol.fields.get(key) and protocol.fields[key].value for key in ("verification_date", "type_number")
        ):
            return
        row = QHBoxLayout()
        self.year_selector = QComboBox()
        self.year_selector.setAccessibleName("К какому прибору относится год выпуска")
        self.year_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.year_selector.addItem("Вся партия", None)
        instruments = {r.instrument.serial: r.instrument for r in self.records if r.instrument.serial}
        for serial in instruments:
            self.year_selector.addItem(f"№ {serial}", serial)
        overrides = sum((YEAR_SCOPE, individual_key(protocol, serial)) in self.window.confirmations
                        for serial in instruments)
        self.year_selector.setCurrentIndex(max(0, self.year_selector.findData(target)))
        self.year_input = QLineEdit()
        self.year_input.setAccessibleName("Год выпуска")
        self.year_input.setPlaceholderText("ГГГГ")
        self.year_input.setMaxLength(32)
        self.year_input.setMaximumWidth(105)
        year_source = label("")
        year_save = button("Сохранить год")
        year_save.setAutoDefault(False)
        row.addWidget(self.year_selector, 1)
        row.addWidget(self.year_input)
        row.addWidget(year_save)
        box.addLayout(row)
        box.addWidget(year_source)

        def show_value():
            instrument = instruments.get(self.year_selector.currentData())
            year = manufacture_year(protocol, self.window.confirmations, instrument)
            self.year_input.setText(str(year.value) if year.value is not None else "")
            description = ("Предполагаемый год — год поверки." if year.source.startswith("Предполагаемый") else
                           year.source + "." if year.source else "Год не указан. Его можно оставить пустым.")
            if instrument is None and overrides:
                description += f" Индивидуальный ввод: {overrides} СИ."
            year_source.setText(description)

        def save_value():
            instrument = instruments.get(self.year_selector.currentData())
            subject = individual_key(protocol, instrument.serial) if instrument else protocol_key(protocol)
            try:
                self.window.storage.save_confirmation(YEAR_SCOPE, subject, {"manufacture_year": self.year_input.text().strip()},
                                                      "Ручной ввод года изготовления", getuser())
            except sqlite3.Error:
                year_source.setText("Не удалось сохранить год. Проверьте доступ к папке данных.")
                return
            self.year_input.setModified(False)
            self.window.recheck()

        self.year_selector.currentIndexChanged.connect(show_value)
        year_save.clicked.connect(save_value)
        show_value()
        if draft is not None:
            self.year_input.setText(draft)
            self.year_input.setModified(True)

    def show_issues(self, box, protocol):
        issues = dict.fromkeys(issue for record in self.records for issue in record.shared_issues)
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
        if issues:
            box.addWidget(section_label("Что нужно исправить"))
            box.addWidget(label("\n".join(f"• {issue}" for issue in issues)))
        if protocol.notices:
            box.addWidget(label("\n".join(dict.fromkeys(protocol.notices))))
