from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from getpass import getuser
from html import escape
import logging
from pathlib import Path
import re
import sqlite3

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMainWindow, QMenu,
    QSplitter, QTabWidget, QTextBrowser, QTreeWidget, QVBoxLayout, QWidget,
)

from ..arshin import export, result_element
from ..confirmations import GUARDS, get_confirmation, with_guards
from ..domain import ERROR, OUTCOMES, READY, REVIEW, UNSUPPORTED
from ..normalization import metrologist, serial_list
from ..readers import EXTENSIONS, read_protocol
from ..registry import EquipmentLookup, iso_date, lookup_equipment, lookup_key, resolve
from ..validation import (
    INDIVIDUAL_FIELDS, LABELS, SHARED_FIELDS, check, individual_key, modification_key, protocol_key, valid_modification,
)
from .common import Form, button, confirmation_values, display, label, message, selected, table
from .dialogs import EquipmentReviewDialog, HistoryDialog, RegistryDialog, XmlDialog, tree_items
from .style import MUTED, SUCCESS

KINDS = {"1": "Первичная", "2": "Периодическая"}


class ImportThread(QThread):
    imported = Signal(object)

    def __init__(self, paths, parent):
        super().__init__(parent)
        self.paths = paths

    def run(self):
        self.imported.emit([read_protocol(path) for path in self.paths])


class RegistryCheckThread(QThread):
    assessed = Signal(object)

    def __init__(self, requests, parent):
        super().__init__(parent)
        self.requests = requests

    def run(self):
        def verify(request):
            item, day = request
            try:
                return lookup_equipment(item, day)
            except Exception:
                logging.exception("Ошибка проверки публичной карточки")
                return EquipmentLookup("unavailable", "Онлайн-проверка: не удалось проверить публичную карточку")

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assessed.emit(dict(zip(self.requests, executor.map(verify, self.requests.values()))))


class MainWindow(QMainWindow):
    def __init__(self, storage, schema, *, online=True):
        super().__init__()
        self.storage = storage
        self.schema = schema
        self.protocols = []
        self.results = []
        self.thread = None
        self.lookup_thread = None
        self.lookups = {}
        self.online = online
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
        self.check_btn = button("Проверить данные", self.start_registry_checks)
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
        additional = button("Дополнительно")
        menu = QMenu(additional)
        menu.addAction("Справочник ФИФ ОЕИ", self.registry)
        menu.addAction("История XML", self.history)
        menu.addAction("Открыть XML…", self.pick_xml)
        menu.addAction("Подтвердить результаты партии…", self.confirm_batch)
        menu.addAction("Все поля протокола…", lambda: self.edit_protocol(all_fields=True))
        menu.addAction("Диагностический журнал", self.open_log)
        menu.addAction("Папка данных и настройки", self.open_data)
        additional.setMenu(menu)
        filters.addWidget(additional)
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
        self.batch_btn = button("Проверить оборудование протокола", self.review_equipment)
        self.protocol_issues_btn = button("К замечаниям протокола", self.show_protocol)
        self.remove_btn = button("Убрать протокол из списка", self.remove_protocol)
        for item in (self.confirm_btn, self.mod_btn, self.batch_btn, self.protocol_issues_btn,
                     self.view_record_btn, self.remove_btn):
            side.addWidget(item)
        split.addWidget(sidebar)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([860, 445])
        box.addWidget(split, 1)
        footer = QHBoxLayout()
        self.notice = label("")
        footer.addWidget(self.notice, 1)
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
        if self.thread is not None or self.lookup_thread is not None:
            self.notice.setText("Дождитесь окончания проверки.")
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
        if self.thread is not None or self.lookup_thread is not None:
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
            if not protocol.error:
                self.storage.migrate_confirmation(protocol)
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
        self.start_registry_checks()

    def start_registry_checks(self):
        if self.thread is not None or self.lookup_thread is not None:
            return
        requests = {}
        for protocol in self.protocols:
            if protocol.error or not protocol.instruments:
                continue
            when = iso_date(protocol.fields["verification_date"].value)
            if when:
                for item in protocol.equipment:
                    requests[lookup_key(item, when.isoformat())] = item, when
        if not requests or not self.online:
            self.recheck()
            return
        self.lookup_thread = RegistryCheckThread(requests, self)
        self.lookups = {}
        self.lookup_thread.assessed.connect(self.accept_registry_checks)
        self.lookup_thread.finished.connect(self.registry_checks_finished)
        self.notice.setText(f"Поиск оборудования в ФИФ ОЕИ · позиций для проверки: {len(requests)}")
        self.recheck()
        self.lookup_thread.start()

    def accept_registry_checks(self, result):
        self.lookups = result
        counts = defaultdict(int)
        for lookup in result.values():
            counts[lookup.state] += 1
        self.notice.setText(f"ФИФ ОЕИ · найдено однозначных карточек: {counts['found']} · "
                            f"требуют внимания: {len(result) - counts['found']}")

    def registry_checks_finished(self):
        self.lookup_thread.deleteLater()
        self.lookup_thread = None
        self.recheck()

    def recheck(self):
        self.confirmations = self.storage.confirmations()
        self.entries = self.storage.entries()
        self.results = check(self.protocols, self.confirmations, self.entries, self.lookups)
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
                     next((r.blockers[0] for r in batch if r.blockers), "") or next(iter(protocol.notices), ""))
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
        busy = self.thread is not None or self.lookup_thread is not None
        self.add_btn.setEnabled(not busy)
        self.folder_btn.setEnabled(not busy)
        self.check_btn.setEnabled(bool(self.protocols) and not busy)
        self.preview_btn.setEnabled(bool(self.results))
        self.export_btn.setEnabled(ready > 0 and not busy)
        self.export_btn.setToolTip(
            "Дождитесь окончания автоматической проверки." if busy else
            "Сначала устраните замечания к данным." if not ready else
            "Сформировать пакет из готовых результатов всей рабочей области."
        )
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
        self.view_record_btn.setEnabled(bool(is_result and current and not current.blockers))
        self.batch_btn.setVisible(not is_result)
        self.batch_btn.setEnabled(bool(valid and protocol.equipment and not self.lookup_thread))
        self.protocol_issues_btn.setVisible(bool(is_result and current and current.shared_issues))
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
            list(batch[0].shared_issues) if batch else protocol.issues + ([protocol.serial_issue] if protocol.serial_issue else [])
        ))
        parts = [f'<p style="color:{MUTED}">{escape(str(protocol.path))}</p>']
        if is_result:
            if current.shared_issues:
                parts.append(f"<p><b>Замечания по протоколу:</b> {len(current.shared_issues)}. "
                             "Оборудование и общие сведения проверяются один раз для партии.</p>")
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
            if any(r.issues for r in batch):
                counts = defaultdict(int)
                for record in batch:
                    for problem in record.issues:
                        counts[problem] += 1
                parts.append("<h3>Уточнения по приборам</h3><ul>" +
                             "".join(f"<li>{escape(problem)} · {count} СИ</li>" for problem, count in counts.items()) +
                             "</ul>")
            for notice in protocol.notices:
                parts.append(f"<p><b>Сведения:</b> {escape(notice)}</p>")
        if issues:
            parts.append("<h3>Что нужно проверить</h3><ul>" +
                         "".join(f"<li>{escape(text)}</li>" for text in issues) + "</ul>")
        elif batch and not any(r.blockers for r in batch):
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
        if not is_result:
            parts.append("<h3>Средства поверки</h3>")
            when = iso_date(protocol.fields["verification_date"].value)
            for item in protocol.equipment:
                live = self.lookups.get(lookup_key(item, when.isoformat())) if when else None
                entry, _ = resolve(item, self.entries, when, live)
                status = (f"Проверено по ФИФ ОЕИ · до {entry.valid_until}" if entry and live and live.state in {"found", "choice"}
                          else f"Локальное подтверждение · до {entry.valid_until}" if entry else "Требует внимания")
                parts.append(f"<p>{escape(item.description)}<br><small>{escape(status)}</small></p>")
        self.card.setHtml("".join(parts))

    def show_results(self):
        self.tabs.setCurrentIndex(1)

    def show_protocol(self):
        protocol = self._protocol()
        self.tabs.setCurrentIndex(0)
        for index, row in enumerate(self.source_model.rows):
            if row[0] is protocol:
                self.source.selectRow(index)
                self.source.scrollTo(self.source_model.index(index, 0))
                break

    def review_equipment(self):
        protocol = self._protocol()
        if protocol:
            EquipmentReviewDialog(self, self.storage, protocol, self.lookups).exec()
            self.recheck()

    def protocol_results(self, *_):
        protocol = selected(self.source)
        if protocol and self.group[id(protocol)]:
            self.show_results()
            for i, row in enumerate(self.result_model.rows):
                if row[0].protocol is protocol:
                    self.instruments.selectRow(i)
                    self.instruments.scrollTo(self.result_model.index(i, 0))
                    break

    def _edit(self, scope, key, title, guidance, fields, baseline, validate, *, protocol=None, instrument=None):
        stored = (get_confirmation(self.confirmations, scope, protocol, instrument) if protocol
                  else self.confirmations.get((scope, key)))
        existing, evidence, confirmer, _ = stored or ({}, "", "", "")
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
            changes = {**existing, **{name: value for name, value in data.items() if
                                     name in existing or value != initial[name] or name == "modification"}}
            if protocol:
                changes = with_guards(protocol, changes, instrument)
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
                       INDIVIDUAL_FIELDS, {name: result.value(name) for name, *_ in INDIVIDUAL_FIELDS}, validate,
                       protocol=protocol, instrument=result.instrument)
            return

        self.edit_protocol()

    def edit_protocol(self, *, all_fields=False):
        protocol = self._protocol()
        if not protocol or protocol.error:
            return

        def validate(values):
            if values.get("metrologist"):
                metrologist(values["metrologist"])
            if values.get("valid_date") and not iso_date(values["valid_date"]):
                raise ValueError("Введите дату в формате ГГГГ-ММ-ДД.")
            if values.get("sign_cipher") and not re.fullmatch(r"[А-Я]{1,3}", values["sign_cipher"]):
                raise ValueError("Шифр знака должен содержать 1–3 заглавные кириллические буквы А–Я.")
            if values.get("explicit_serials"):
                numbers = serial_list(values["explicit_serials"], explicit=True)
                if numbers != [i.serial for i in protocol.instruments] or len(numbers) != protocol.declared:
                    raise ValueError("Точный список должен совпасть по порядку и количеству с измерениями Excel.")

        baseline = {k: item.value for k, item in protocol.fields.items()}
        baseline.update(sign_pass=None, sign_mi=None)
        common = {"verification_type", "sign_cipher", "calibration", "metrologist",
                  "valid_date", "valid_date_absent", "sign_pass", "sign_mi"}
        common.update(key for key in ("temperature", "pressure", "humidity") if not baseline.get(key))
        if protocol.elements == 2:
            common.add("one_result")
        if protocol.serial_issue:
            common.add("explicit_serials")
        fields = SHARED_FIELDS if all_fields else [field for field in SHARED_FIELDS if field[0] in common]
        self._edit("protocol", protocol_key(protocol), "Общие сведения протокола",
                   f"{protocol.number or protocol.path.name}. Изменения применяются к приборам этой книги.",
                   fields, baseline, validate, protocol=protocol)

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
            message(self, "Подтверждение партии", "Подходящих записей с неподтверждённым заключением нет.")
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
            keys = {individual_key(protocol, r.instrument.serial): with_guards(
                protocol, {"outcome": "pass"}, r.instrument)[GUARDS]["outcome"] for r in pending}
            self.storage.save_batch_outcome(keys, evidence, confirmer)
            self.storage.set_setting("confirmer", confirmer)
            self.recheck()

    def preview_record(self):
        result = self._active()
        if not result or result.blockers:
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

    def open_log(self):
        log = self.storage.path.parent / "diagnostics.log"
        if log.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log)))
        else:
            message(self, "Диагностический журнал", "В журнале пока нет записей.")

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
        ready = [result for result in self.results if not result.blockers]
        if not ready:
            return
        excluded = [{"protocol": r.protocol.number or r.protocol.path.name, "serial": r.instrument.serial,
                     "issues": list(r.blockers)} for r in self.results if r.blockers]
        excluded.extend({"protocol": p.number or p.path.name, "serial": "", "issues":
                         [p.error] if p.error else p.issues + [p.serial_issue or "Нет отдельных СИ."]}
                        for p in self.protocols if not self.group[id(p)])
        repeated = [f"{r.protocol.number} / {r.instrument.serial}" for r in ready if r.identity in self.previously]
        if excluded or repeated:
            decision = self.confirm_export_scope(ready, excluded, repeated)
            if decision is None:
                return
            evidence, confirmer = decision
        else:
            evidence, confirmer = "Все результаты проверены", f"Сеанс Windows: {getuser()}"
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
        if excluded or repeated:
            self.storage.set_setting("confirmer", confirmer)
        self.notice.setText(f"Сформировано файлов: {len(files)} · {files[0].parent}")
        self.recheck()
        context = {(r.instrument.serial, r.value("verification_date"), r.value("type_number")): r.protocol.number
                   for r in ready}
        XmlDialog(self, files, self.schema, context).exec()

    def confirm_export_scope(self, ready, excluded, repeated):
        form = Form(self, "Сформировать XML",
                    f"В пакет войдут {len(ready)} проверенных результатов всей рабочей области. "
                    f"Исключено: {len(excluded)}. Повторно включается: {len(repeated)}.",
                    size=(770, 590), save="Выбрать папку")
        if excluded:
            grouped = defaultdict(lambda: defaultdict(list))
            for entry in excluded:
                for issue in entry["issues"]:
                    grouped[entry["protocol"]][issue].append(entry["serial"])
            text = "\n\n".join(
                f"{protocol}\n" + "\n".join(f"• {issue}" + (f" · {', '.join(owners)}" if all(owners) else "")
                                              for issue, owners in problems.items())
                for protocol, problems in grouped.items()
            )
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
        return confirmation_values(form, form.values()) if form.exec() else None
