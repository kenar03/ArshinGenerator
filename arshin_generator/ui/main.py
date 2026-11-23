from collections import defaultdict
from html import escape
import logging
from pathlib import Path
import re
import sqlite3

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMainWindow,
    QSplitter, QTabWidget, QTextBrowser, QTreeWidget, QVBoxLayout, QWidget,
)

from ..arshin import export, result_element
from ..domain import ERROR, OUTCOMES, READY, REVIEW, UNSUPPORTED
from ..normalization import metrologist, serial_list
from ..readers import EXTENSIONS, read_protocol
from ..registry import iso_date
from ..validation import (
    INDIVIDUAL_FIELDS, LABELS, SHARED_FIELDS, check, individual_key, modification_key, protocol_key, valid_modification,
)
from .common import Form, button, confirmation_values, display, label, message, selected, table
from .dialogs import HistoryDialog, RegistryDialog, XmlDialog, tree_items
from .style import MUTED, SUCCESS

KINDS = {"1": "Первичная", "2": "Периодическая"}


class ImportThread(QThread):
    imported = Signal(object)

    def __init__(self, paths, parent):
        super().__init__(parent)
        self.paths = paths

    def run(self):
        self.imported.emit([read_protocol(path) for path in self.paths])


class MainWindow(QMainWindow):
    def __init__(self, storage, schema):
        super().__init__()
        self.storage = storage
        self.schema = schema
        self.protocols = []
        self.results = []
        self.thread = None
        self.setWindowTitle("Аршин · Генератор XML")
        self.setMinimumSize(1010, 620)
        self.resize(1370, 815)
        self.setAcceptDrops(True)
        central = QWidget()
        self.setCentralWidget(central)
        box = QVBoxLayout(central)
        box.setContentsMargins(21, 18, 21, 14)
        box.setSpacing(12)
        mast = QFrame(objectName="mast")
        head = QHBoxLayout(mast)
        head.setContentsMargins(22, 14, 22, 15)
        branding = QVBoxLayout()
        branding.setSpacing(2)
        branding.addWidget(label("Аршин · Генератор XML", "title"))
        branding.addWidget(label("Пакетная подготовка результатов поверки ТСП и ТСМ", "subtitle"))
        head.addLayout(branding, 2)
        self.summary = label("", "summary")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.summary, 1)
        box.addWidget(mast)
        row = QHBoxLayout()
        self.add_btn = button("Добавить протоколы", self.pick_files)
        self.folder_btn = button("Добавить папку", self.pick_folder)
        self.check_btn = button("Проверить данные", self.recheck)
        self.preview_btn = button("Предварительный просмотр", self.show_results)
        self.export_btn = button("Сформировать XML", self.export_xml, primary=True)
        for item in (self.add_btn, self.folder_btn, self.check_btn, self.preview_btn):
            row.addWidget(item)
        row.addStretch()
        row.addWidget(self.export_btn)
        box.addLayout(row)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Поиск по протоколу, заводскому № или модификации")
        self.filter = QComboBox()
        self.filter.addItems(["Все статусы", READY, REVIEW, ERROR, UNSUPPORTED])
        filters.addWidget(self.search, 2)
        filters.addWidget(self.filter)
        filters.addStretch(1)
        filters.addWidget(button("Справочник ФИФ ОЕИ", self.registry))
        filters.addWidget(button("История XML", self.history))
        filters.addWidget(button("Открыть XML…", self.pick_xml))
        box.addLayout(filters)
        split = QSplitter()
        self.tabs = QTabWidget()
        self.source, self.source_model = table(
            ("Протокол / файл", "Вид поверки", "Физических СИ", "Готово", "Статус", "Замечание"),
            (165, 100, 110, 67, 144)
        )
        self.instruments, self.result_model = table(
            ("Протокол", "Заводской №", "Модификация", "Дата", "Заключение", "Статус"),
            (145, 105, 145, 103, 116)
        )
        self.tabs.addTab(self.source, "Протоколы")
        self.tabs.addTab(self.instruments, "Приборы")
        split.addWidget(self.tabs)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(3, 0, 0, 0)
        self.card_title = label("Сведения о выбранной записи", "heading")
        self.card_title.setMinimumHeight(36)
        side.addWidget(self.card_title)
        self.card = QTextBrowser()
        self.card.setOpenLinks(False)
        side.addWidget(self.card, 1)
        self.confirm_btn = button("Уточнить данные", self.edit_selection, primary=True)
        self.mod_btn = button("Подтвердить модификацию", self.edit_modification)
        self.view_record_btn = button("Поля будущего XML", self.preview_record)
        self.batch_btn = button("Подтвердить пригодные СИ одной партией", self.confirm_batch)
        self.remove_btn = button("Убрать протокол из списка", self.remove_protocol)
        for item in (self.confirm_btn, self.mod_btn, self.batch_btn, self.view_record_btn, self.remove_btn):
            side.addWidget(item)
        split.addWidget(sidebar)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([860, 445])
        box.addWidget(split, 1)
        footer = QHBoxLayout()
        self.notice = label("")
        footer.addWidget(self.notice, 1)
        footer.addWidget(button("Папка данных", self.open_data))
        box.addLayout(footer)
        self.search.textChanged.connect(self.render)
        self.filter.currentIndexChanged.connect(self.render)
        self.tabs.currentChanged.connect(self.update_card)
        self.source.selectionModel().currentChanged.connect(self.update_card)
        self.instruments.selectionModel().currentChanged.connect(self.update_card)
        self.source.doubleClicked.connect(self.protocol_results)
        self.instruments.doubleClicked.connect(self.edit_selection)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.pick_files)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search.setFocus)
        self.recheck()

    def _active(self):
        return selected(self.source if self.tabs.currentIndex() == 0 else self.instruments)

    def _protocol(self):
        current = self._active()
        return current if self.tabs.currentIndex() == 0 else current.protocol if current else None

    def closeEvent(self, event):
        if self.thread is not None:
            self.notice.setText("Дождитесь окончания чтения протоколов.")
            event.ignore()
        else:
            super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_paths([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Добавить протоколы Excel", self.storage.setting("input_folder"),
            "Книги Excel (*.xlsm *.xlsx *.xltm *.xltx *.xls *.xlsb);;Все файлы (*)"
        )
        if paths:
            self.import_paths([Path(path) for path in paths])

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку с протоколами",
                                                   self.storage.setting("input_folder"))
        if folder:
            self.import_paths([Path(folder)])

    def import_paths(self, paths):
        if self.thread is not None:
            return
        files = []
        count_ignored = 0
        for path in paths:
            if path.is_dir():
                try:
                    contents = [p for p in path.iterdir() if p.is_file() and not p.name.startswith("~$")]
                except OSError:
                    message(self, "Чтение папки", f"Не удалось прочитать папку {path}.", error=True)
                    continue
                excel = [p for p in contents if p.suffix.lower() in EXTENSIONS | {".xls", ".xlsb"}]
                count_ignored += len(contents) - len(excel)
                files.extend(sorted(excel))
            elif not path.name.startswith("~$"):
                files.append(path)
        if not files:
            self.notice.setText("В выбранной папке нет книг Excel.")
            return
        self.storage.set_setting("input_folder", str(files[0].parent))
        self.notice.setText(f"Чтение книг Excel… {len(files)}")
        self.folder_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.thread = ImportThread(list(dict.fromkeys(files)), self)
        self.thread.imported.connect(self.append_protocols)
        self.thread.finished.connect(self.import_finished)
        self.ignored = count_ignored
        self.thread.start()

    def append_protocols(self, incoming):
        duplicates = 0
        for protocol in incoming:
            self.storage.audit("Чтение протокола", {
                "path": str(protocol.path), "digest": protocol.digest, "reader": protocol.reader,
                "number": protocol.number, "error": protocol.error,
            })
            same_path = next((p for p in self.protocols if p.path == protocol.path), None)
            if any(p.digest == protocol.digest and protocol.digest and p is not same_path for p in self.protocols):
                duplicates += 1
                continue
            if same_path:
                self.protocols[self.protocols.index(same_path)] = protocol
            else:
                self.protocols.append(protocol)
        if self.protocols:
            self.tabs.setCurrentIndex(0)
        self.notice.setText(f"Обработано книг: {len(incoming)}. " +
                            (f"Одинаковые копии пропущены: {duplicates}. " if duplicates else "") +
                            (f"Файлы других типов в папке: {self.ignored}." if self.ignored else ""))

    def import_finished(self):
        self.thread.deleteLater()
        self.thread = None
        self.add_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
        self.recheck()

    def recheck(self):
        self.confirmations = self.storage.confirmations()
        self.results = check(self.protocols, self.confirmations, self.storage.entries())
        self.previously = self.storage.seen([r.identity for r in self.results])
        self.group = defaultdict(list)
        for item in self.results:
            self.group[id(item.protocol)].append(item)
        self.render()

    def render(self, *_):
        word = self.search.text().strip().casefold()
        state = self.filter.currentText()
        old_source = selected(self.source)
        old_instrument = selected(self.instruments)
        sources = []
        records = []
        ready = 0
        for protocol in self.protocols:
            batch = self.group[id(protocol)]
            good = sum(r.status == READY for r in batch)
            ready += good
            status = UNSUPPORTED if protocol.unsupported else ERROR if protocol.error else (
                READY if batch and good == len(batch) else REVIEW
            )
            title = protocol.number or protocol.path.name
            kind = next((r.value("verification_type") for r in batch), None)
            issue = (protocol.error or next(iter(protocol.issues), "") or protocol.serial_issue or
                     next((r.issues[0] for r in batch if r.issues), ""))
            protocol_match = word in f"{title} {protocol.path.name}".casefold()
            if (state == "Все статусы" or state == status) and (
                protocol_match or any(word in self._match_text(r) for r in batch)
            ):
                sources.append((protocol, (title, KINDS.get(kind, "—"), protocol.declared, good, status, issue)))
            for result in batch:
                if (state == "Все статусы" or state == result.status) and (
                    protocol_match or word in self._match_text(result)
                ):
                    result_title = OUTCOMES.get(result.value("outcome") or "", "Не подтверждён")
                    if not result.value("outcome") and result.candidate:
                        result_title = OUTCOMES[result.candidate] + "?"
                    records.append((result, (
                        title, result.instrument.serial or result.instrument.raw,
                        result.value("modification") or protocol.proposed_modification,
                        result.value("verification_date"), result_title, result.status
                    )))
        self.source_model.update(sources)
        self.result_model.update(records)
        self.summary.setText(f"Протоколы  {len(self.protocols)}    Приборы  {len(self.results)}    Готово  {ready}")
        self.check_btn.setEnabled(bool(self.protocols))
        self.preview_btn.setEnabled(bool(self.results))
        self.export_btn.setEnabled(ready > 0 and self.thread is None)
        self.export_btn.setToolTip("Сначала устраните замечания к данным." if not ready else
                                   "Сформировать пакет из готовых результатов всей рабочей области.")
        if sources:
            index = next((i for i, row in enumerate(sources) if row[0] is old_source), 0)
            self.source.selectRow(index)
        if records:
            index = next((i for i, row in enumerate(records)
                          if old_instrument and row[0].instrument is old_instrument.instrument), 0)
            self.instruments.selectRow(index)
        self.update_card()

    @staticmethod
    def _match_text(result):
        return " ".join((result.instrument.serial, result.instrument.raw,
                         str(result.value("modification") or result.protocol.proposed_modification))).casefold()

    def update_card(self, *_):
        current = self._active()
        protocol = self._protocol()
        is_result = self.tabs.currentIndex() == 1
        valid = bool(protocol and not protocol.error)
        self.confirm_btn.setEnabled(bool(valid and (not is_result or current.instrument.serial)))
        self.confirm_btn.setText("Уточнить результат прибора" if is_result else "Уточнить сведения протокола")
        self.mod_btn.setVisible(not is_result)
        self.mod_btn.setEnabled(bool(valid and protocol.fields.get("marking") and protocol.fields["marking"].value))
        self.view_record_btn.setVisible(is_result)
        self.view_record_btn.setEnabled(bool(is_result and current and not current.issues))
        self.batch_btn.setVisible(not is_result)
        self.batch_btn.setEnabled(bool(valid and any(
            r.candidate == "pass" and not r.value("outcome") for r in self.group[id(protocol)]
        )))
        self.remove_btn.setVisible(not is_result)
        self.remove_btn.setEnabled(bool(protocol))
        if not protocol:
            self.card_title.setText("Рабочая область")
            self.card.setHtml("<h3>Добавьте протоколы Excel</h3>"
                              "<p>Выберите готовые книги или перетащите их сюда из Проводника.</p>"
                              "<p>После чтения проверьте замечания на вкладках «Протоколы» и «Приборы».</p>"
                              if not self.protocols else "<p>По выбранному фильтру записей нет.</p>")
            return
        self.card_title.setText(f"Прибор {current.instrument.serial or 'без номера'}" if is_result else
                                protocol.number or "Протокол")
        if protocol.error:
            self.card.setPlainText(f"{protocol.path}\n\n{protocol.error}")
            return
        batch = self.group[id(protocol)]
        issues = current.issues if is_result else list(dict.fromkeys(
            protocol.issues + ([protocol.serial_issue] if protocol.serial_issue and not batch else []) +
            [issue for r in batch for issue in r.issues]
        ))
        parts = [f'<p style="color:{MUTED}">{escape(str(protocol.path))}</p>']
        if is_result:
            if current.identity in self.previously:
                parts.append("<p><b>Этот результат ранее включался в XML.</b></p>")
            parts.append(f"<p><b>Заводской номер</b> {escape(display(current.instrument.serial))}<br>"
                         f"Исходный № {escape(current.instrument.raw)} · {escape(current.instrument.source)}</p>")
            if current.candidate and not current.value("outcome"):
                parts.append(f"<p><b>Кандидат заключения</b> {OUTCOMES[current.candidate]}</p>")
            for e in current.instrument.elements:
                parts.append(f"<p><b>ЧЭ {e.index}</b> {escape(display(e.status))}<br>"
                             f"{'Измерения сохранены' if e.complete else 'Измерения неполные'} · {escape(e.source)}</p>")
        else:
            parts.append(f"<p><b>Из Excel</b> · {len(batch)} физических СИ, ЧЭ на прибор: "
                         f"{display(protocol.elements or None)}.</p>")
            if protocol.evidence:
                parts.append(f"<p>{escape(protocol.evidence)} <small>Отчёт!A45</small></p>")
        if issues:
            parts.append("<h3>Что нужно проверить</h3><ul>" +
                         "".join(f"<li>{escape(text)}</li>" for text in issues) + "</ul>")
        elif batch:
            parts.append(f'<p style="color:{SUCCESS}"><b>Данные проверены</b></p>')
        fields = current.fields if is_result else (batch[0].fields if batch else protocol.fields)
        parts.append("<h3>Сведения и источники</h3>")
        if not fields.get("modification") and protocol.proposed_modification:
            parts.append(f"<p><b>Кандидат модификации</b> {escape(protocol.proposed_modification)}</p>")
        for name, datum in fields.items():
            if name in {field[0] for field in INDIVIDUAL_FIELDS} and not is_result:
                continue
            value = (OUTCOMES.get(datum.value, display(datum.value)) if name == "outcome" else
                     KINDS.get(datum.value, display(datum.value)) if name == "verification_type" else display(datum.value))
            raw = f"<br>Исходное значение: {escape(datum.raw)}" if datum.raw and datum.raw != value else ""
            parts.append(f"<p><b>{escape(LABELS.get(name, name))}</b> {escape(value)}"
                         f"<br><small>{escape(datum.source)}{raw}</small></p>")
        parts.append("<h3>Средства поверки</h3>")
        for item in protocol.equipment:
            parts.append(f"<p>{escape(item.description)}<br><small>{escape(item.source)}"
                         f"{' · ' + escape(item.valid_hint) if item.valid_hint else ''}</small></p>")
        for entry in (current.equipment if is_result else batch[0].equipment if batch else ()):
            parts.append(f"<p><b>Подтверждена карточка ФИФ ОЕИ</b><br>{escape(entry.name)} · "
                         f"{escape(entry.identifier or (entry.type_number + ' / ' + entry.registry_serial))}<br>"
                         f"<small>{escape(entry.evidence)} · {escape(entry.confirmer)} · {escape(entry.confirmed_at)}"
                         f" · действует до {escape(entry.valid_until)}</small></p>")
        self.card.setHtml("".join(parts))

    def show_results(self):
        self.tabs.setCurrentIndex(1)

    def protocol_results(self, *_):
        protocol = selected(self.source)
        if protocol and self.group[id(protocol)]:
            self.show_results()
            for i, row in enumerate(self.result_model.rows):
                if row[0].protocol is protocol:
                    self.instruments.selectRow(i)
                    self.instruments.scrollTo(self.result_model.index(i, 0))
                    break

    def _edit(self, scope, key, title, guidance, fields, baseline, validate):
        existing, evidence, confirmer, _ = self.confirmations.get((scope, key), ({}, "", "", ""))
        form = Form(self, title, guidance)
        for name, title, kind in fields:
            form.field(name, title, kind, existing.get(name, baseline.get(name)))
        initial = form.values()
        form.confirmation(confirmer or self.storage.setting("confirmer"), evidence)

        def verify(data):
            confirmation_values(form, data)
            validate(data)

        form.validate = verify
        if form.exec():
            data = form.values()
            evidence, confirmer = confirmation_values(form, data)
            required = {"outcome", "modification"}
            if data.get("outcome") == "pass":
                required.update(("sign_mi", "sign_pass"))
            changes = {name: value for name, value in data.items() if
                       name in existing or value != initial[name] or name in required}
            self.storage.save_confirmation(scope, key, changes, evidence, confirmer)
            self.storage.set_setting("confirmer", confirmer)
            self.recheck()

    def edit_selection(self, *_):
        protocol = self._protocol()
        if not protocol or protocol.error:
            return
        if self.tabs.currentIndex() == 1:
            result = selected(self.instruments)
            if not result or not result.instrument.serial:
                return

            def validate(values):
                if not values["outcome"]:
                    raise ValueError("Выберите окончательное заключение по данному прибору.")
                if values["sticker"] and not re.fullmatch(r"[0-9]{11}", values["sticker"]):
                    raise ValueError("Номер наклейки должен состоять из 11 цифр.")
                if values["outcome"] == "pass" and result.candidate == "fail":
                    raise ValueError("Сохранённый результат ЧЭ противоречит положительному заключению.")

            hint = OUTCOMES[result.candidate]
            self._edit("instrument", individual_key(protocol, result.instrument.serial),
                       f"Результат прибора {result.instrument.serial}",
                       f"{protocol.number} · По сохранённым данным: {hint}. "
                       "Укажите окончательный результат и сверьте реквизиты и знаки по завершённому документу.",
                       INDIVIDUAL_FIELDS, {name: result.value(name) for name, *_ in INDIVIDUAL_FIELDS}, validate)
            return

        def validate(values):
            if values["metrologist"]:
                metrologist(values["metrologist"])
            if values["valid_date"] and not iso_date(values["valid_date"]):
                raise ValueError("Введите дату в формате ГГГГ-ММ-ДД.")
            if values["sign_cipher"] and not re.fullmatch(r"[А-Я]{1,3}", values["sign_cipher"]):
                raise ValueError("Шифр знака должен содержать 1–3 заглавные кириллические буквы А–Я.")
            if values["explicit_serials"]:
                numbers = serial_list(values["explicit_serials"], explicit=True)
                if numbers != [i.serial for i in protocol.instruments] or len(numbers) != protocol.declared:
                    raise ValueError("Точный список должен совпасть по порядку и количеству с измерениями Excel.")

        self._edit("protocol", protocol_key(protocol), "Общие сведения протокола",
                   f"{protocol.number or protocol.path.name}. Изменения применяются к приборам этой книги.",
                   SHARED_FIELDS, {k: item.value for k, item in protocol.fields.items()}, validate)

    def edit_modification(self):
        protocol = self._protocol()
        if not protocol or not protocol.fields.get("marking"):
            return
        marking = protocol.fields["marking"].value
        kind = protocol.fields["type_number"].value

        def validate(values):
            if not valid_modification(protocol, values["modification"]):
                qualifier = " с префиксом " + " или ".join(protocol.modification_prefixes) if protocol.modification_prefixes else ""
                raise ValueError(f"Укажите подтверждённую модификацию{qualifier}.")

        self._edit("modification", modification_key(protocol), "Модификация из описания типа",
                   f"№ типа {display(kind)} · Маркировка {display(marking)}. "
                   f"Кандидат: {display(protocol.proposed_modification)}. "
                   "Подтверждение сохраняется для этой точной пары типа и маркировки.",
                   (("modification", LABELS["modification"], "text"),),
                   {"modification": protocol.proposed_modification}, validate)

    def confirm_batch(self):
        protocol = self._protocol()
        if not protocol:
            return
        pending = [r for r in self.group[id(protocol)] if r.candidate == "pass" and
                   not r.value("outcome") and not r.instrument.problem and r.instrument.serial]
        if not pending or len({r.instrument.serial for r in pending}) != len(pending):
            return
        form = Form(self, "Окончательное заключение по партии",
                    f"{protocol.number} · Признайте пригодными указанные СИ, только если заключение "
                    "для каждого из них подтверждено завершённым документом. "
                    "Наклейки указываются отдельно.", size=(675, 420))
        form.field("_list", "Приборы для подтверждения", "list",
                   ", ".join(r.instrument.serial for r in pending), readonly=True)
        form.field("_ack", "Все указанные СИ признаны пригодными", "bool")
        form.confirmation(self.storage.setting("confirmer"))

        def validate(values):
            confirmation_values(form, values)
            if values["_ack"] is not True:
                raise ValueError("Подтвердите заключение для перечисленных приборов.")

        form.validate = validate
        if form.exec():
            evidence, confirmer = confirmation_values(form, form.values())
            keys = [individual_key(protocol, result.instrument.serial) for result in pending]
            self.storage.save_batch_outcome(keys, evidence, confirmer)
            self.storage.set_setting("confirmer", confirmer)
            self.recheck()

    def preview_record(self):
        result = self._active()
        if not result or result.issues:
            return
        window = QDialog(self)
        window.setWindowTitle(f"Поля XML · {result.instrument.serial}")
        window.resize(760, 640)
        layout = QVBoxLayout(window)
        layout.addWidget(label(f"{result.protocol.number} · {result.instrument.serial}", "heading"))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Поле", "Значение"])
        tree.setColumnWidth(0, 310)
        try:
            tree_items(result_element(result), tree)
        except ValueError:
            message(self, "Поля XML", "Запись содержит символы, которые нельзя записать в XML.", error=True)
            return
        tree.expandAll()
        layout.addWidget(tree)
        layout.addWidget(button("Закрыть", window.accept))
        window.exec()

    def remove_protocol(self):
        protocol = self._protocol()
        if protocol:
            self.protocols.remove(protocol)
            self.recheck()

    def registry(self):
        protocol = self._protocol()
        items = protocol.equipment if protocol else ()
        RegistryDialog(self, self.storage, items).exec()
        self.recheck()

    def history(self):
        HistoryDialog(self, self.storage, self.schema).exec()

    def open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.storage.path.parent)))

    def pick_xml(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Открыть сформированный XML", self.storage.setting("output_folder"), "Файл XML (*.xml)"
        )
        if files:
            paths = [Path(file) for file in files]
            local = {path.resolve() for path in paths}
            context = {}
            for entry in self.storage.history():
                if Path(entry["directory"]).resolve() in {path.parent for path in local}:
                    context.update(self.storage.export_context(entry["id"]))
            XmlDialog(self, paths, self.schema, context).exec()

    def export_xml(self):
        self.recheck()
        ready = [result for result in self.results if not result.issues]
        if not ready:
            return
        excluded = [{"protocol": r.protocol.number or r.protocol.path.name, "serial": r.instrument.serial,
                     "issues": r.issues} for r in self.results if r.issues]
        excluded.extend({"protocol": p.number or p.path.name, "serial": "", "issues":
                         [p.error] if p.error else p.issues + [p.serial_issue or "Нет отдельных СИ."]}
                        for p in self.protocols if not self.group[id(p)])
        repeated = [f"{r.protocol.number} / {r.instrument.serial}" for r in ready if r.identity in self.previously]
        form = Form(self, "Сформировать XML",
                    f"В пакет войдут {len(ready)} проверенных результатов всей рабочей области. "
                    f"Исключено: {len(excluded)}. Повторно включается: {len(repeated)}.",
                    size=(770, 590 if excluded or repeated else 330), save="Выбрать папку")
        if excluded:
            text = "\n\n".join(f"{e['protocol']} / {e['serial'] or 'книга'} · " + " ".join(e["issues"])
                                 for e in excluded)
            form.field("_excluded", "Исключённые записи", "list", text, readonly=True)
            form.field("_partial", "Подтверждаю выгрузку без этих записей", "bool")
        if repeated:
            form.field("_repeated", "Ранее включались в XML", "list", "\n".join(repeated), readonly=True)
            form.field("_repeat", "Подтверждаю повторное формирование", "bool")
        form.confirmation(self.storage.setting("confirmer"))

        def validate(data):
            confirmation_values(form, data)
            if excluded and data["_partial"] is not True:
                raise ValueError("Подтвердите исключение ошибок из пакета.")
            if repeated and data["_repeat"] is not True:
                raise ValueError("Подтвердите повторное включение записей.")

        form.validate = validate
        if not form.exec():
            return
        evidence, confirmer = confirmation_values(form, form.values())
        folder = QFileDialog.getExistingDirectory(self, "Папка для сохранения пакета",
                                                   self.storage.setting("output_folder"))
        if not folder:
            return
        try:
            files = export(ready, self.schema, Path(folder), self.storage, excluded,
                           f"{confirmer}: {evidence}")
        except (ValueError, OSError, sqlite3.Error) as exc:
            logging.exception("Ошибка формирования XML")
            message(self, "Экспорт XML", str(exc) if isinstance(exc, ValueError) else
                    "Не удалось записать и проверить пакет. Проверьте права на папку и состояние базы.", error=True)
            return
        self.storage.set_setting("output_folder", folder)
        self.storage.set_setting("confirmer", confirmer)
        self.notice.setText(f"Сформировано файлов: {len(files)} · {files[0].parent}")
        self.recheck()
        context = {(r.instrument.serial, r.value("verification_date"), r.value("type_number")): r.protocol.number
                   for r in ready}
        XmlDialog(self, files, self.schema, context).exec()
