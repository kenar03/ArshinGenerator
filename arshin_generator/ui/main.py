from collections import defaultdict
from dataclasses import replace
from datetime import date
from getpass import getuser
import logging
from math import ceil
from pathlib import Path
import re
import sqlite3

from PySide6.QtCore import QStandardPaths, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QHeaderView, QMainWindow, QMenu, QToolButton,
    QVBoxLayout, QWidget,
)

from .. import APP_NAME
from ..arshin import automatic_paths, export, format_packets, split_paths
from ..domain import CHECKING, ERROR, READY, REVIEW
from ..readers import EXTENSIONS, read_protocol
from ..registry import iso_date, lookup_key
from ..validation import check, empty_protocol_issues, location
from ..verification import RegistrySession
from .common import apply_dialog_icon, button, fit_window, label, message, selected, table
from .protocol_list import ProtocolHeader, ProtocolTableModel
from .protocols import ProtocolDialog
from .workflow import laboratory_settings


class ImportThread(QThread):
    imported = Signal(object)

    def __init__(self, paths, storage, parent):
        super().__init__(parent)
        self.paths = paths
        self.storage = storage
        self.audit_failed = False

    def run(self):
        protocols = [read_protocol(path) for path in self.paths]
        try:
            self.storage.record_imports(protocols)
        except (sqlite3.Error, ValueError):
            self.audit_failed = True
            logging.exception("Ошибка записи истории импорта")
            return
        self.imported.emit(protocols)


class RegistryCheckThread(QThread):
    assessed = Signal(object)

    def __init__(self, session, requests, type_numbers, parent):
        super().__init__(parent)
        self.requests = requests
        self.type_numbers = type_numbers
        self.session = session

    def run(self):
        self.assessed.emit(self.session.check(self.requests, self.type_numbers, self.isInterruptionRequested))


class MainWindow(QMainWindow):
    checked = Signal()

    def __init__(self, storage, schema, *, online=True, registry_session=None):
        super().__init__()
        self.storage = storage
        self.schema = schema
        self.online = online
        self.registry = registry_session or RegistrySession()
        self.protocols = []
        self.results = []
        self.thread = None
        self.lookup_thread = None
        self.after_registry_check = None
        self.lookups = {}
        self.type_titles = {}
        self.type_issues = {}
        self.retryable_types = set()
        self.expiry_timer = QTimer(self)
        self.expiry_timer.setSingleShot(True)
        self.expiry_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.expiry_timer.timeout.connect(self.recheck)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(680, 440)
        fit_window(self, 900, 600)
        self.setAcceptDrops(True)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(4)
        title.addWidget(label("Протоколы поверки", "workTitle"))
        self.summary = label("")
        title.addWidget(self.summary)
        header.addLayout(title, 1)
        self.settings_btn = button("Настройки", self.laboratory_settings)
        header.addWidget(self.settings_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        actions = QHBoxLayout()
        self.add_btn = QToolButton()
        self.add_btn.setText("Добавить протоколы")
        self.add_btn.clicked.connect(self.pick_files)
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        add = QMenu(self.add_btn)
        add.addAction("Выбрать папку…", self.pick_folder)
        self.add_btn.setMenu(add)
        actions.addWidget(self.add_btn)
        self.remove_btn = button("Удалить выбранные", self.remove_protocols)
        actions.addWidget(self.remove_btn)
        actions.addStretch()
        self.preview_btn = button("Просмотреть", self.show_results)
        self.export_btn = button("Сформировать XML", self.export_xml, primary=True)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.export_btn)
        layout.addLayout(actions)

        self.empty = label("Добавьте протоколы Excel или перетащите их сюда.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty, 1)

        self.source, self.source_model = table(("", "Протокол", "Тип протокола", "Приборов", "Состояние"),
                                               (34, 250, 140, 80, 152), model_class=ProtocolTableModel,
                                               header_class=ProtocolHeader)
        sizes = self.source.horizontalHeader()
        sizes.setStretchLastSection(False)
        sizes.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        sizes.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        sizes.select_all.clicked.connect(self.source_model.set_all_checked)
        self.source_model.marks_changed.connect(self.update_marks)
        self.source.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.source, 1)
        self.source.doubleClicked.connect(self.open_row)

        self.notice = label("")
        layout.addWidget(self.notice)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.pick_files)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self.remove_protocols)
        self.recheck()

    @property
    def busy(self):
        return self.thread is not None or self.lookup_thread is not None

    def set_notice(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def closeEvent(self, event):
        if self.busy:
            self.set_notice("Дождитесь окончания проверки.")
            if self.lookup_thread:
                self.after_registry_check = None
                self.lookup_thread.requestInterruption()
            event.ignore()
        else:
            super().closeEvent(event)

    def dragEnterEvent(self, event):
        if not self.busy and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_paths([Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Выбрать протоколы", self.storage.setting("input_folder"),
                                                "Книги Excel (*.xlsm *.xlsx *.xltm *.xltx *.xls *.xlsb)")
        if paths:
            self.import_paths([Path(path) for path in paths])

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку с протоколами",
                                                   self.storage.setting("input_folder"))
        if folder:
            self.import_paths([Path(folder)])

    def import_paths(self, paths):
        if self.busy:
            return
        files = []
        for path in paths:
            if path.is_dir():
                try:
                    files.extend(sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith("~$")
                                        and p.suffix.lower() in EXTENSIONS | {".xls", ".xlsb"}))
                except OSError:
                    message(self, "Выбор папки", "Не удалось открыть выбранную папку.", error=True)
            elif not path.name.startswith("~$"):
                files.append(path)
        if not files:
            self.set_notice("В выбранной папке нет книг Excel.")
            return
        self.storage.set_setting("input_folder", str(files[0].parent))
        self.set_notice("Читаем протоколы…")
        self.thread = ImportThread(list(dict.fromkeys(path.resolve() for path in files)), self.storage, self)
        self.thread.imported.connect(self.append_protocols)
        self.thread.finished.connect(self.import_finished)
        self.render()
        self.thread.start()

    def append_protocols(self, incoming):
        paths = {protocol.path: index for index, protocol in enumerate(self.protocols)}
        digests = {protocol.digest: index for index, protocol in enumerate(self.protocols) if protocol.digest}
        for protocol in incoming:
            index = paths.get(protocol.path)
            if protocol.digest in digests and digests[protocol.digest] != index:
                continue
            if index is None:
                index = len(self.protocols)
                paths[protocol.path] = index
                self.protocols.append(protocol)
            else:
                digests.pop(self.protocols[index].digest, None)
                self.protocols[index] = protocol
            if protocol.digest:
                digests[protocol.digest] = index

    def import_finished(self):
        audit_failed = self.thread.audit_failed
        self.thread.deleteLater()
        self.thread = None
        if audit_failed:
            self.recheck()
            self.set_notice("Не удалось записать историю импорта. Проверьте доступ к папке данных.")
        else:
            self.start_registry_checks()

    def check_inputs(self):
        self.entries = self.storage.entries()
        self.confirmations = self.storage.confirmations()
        self.laboratory = self.storage.laboratory()
        local = check(self.protocols, self.confirmations, self.entries, laboratory=self.laboratory,
                      check_registry=False, require_public=True)
        self.local_group = defaultdict(list)
        for record in local:
            self.local_group[id(record.protocol)].append(record)
        self.registry_protocols = [
            p for p in self.protocols
            if (records := self.local_group[id(p)]) and self.complete(p, records)
        ]
        self.type_only_protocols = [
            p for p in self.protocols
            if p.modification_issue and self.complete(p, self.local_group[id(p)], ignore=p.modification_issue)
        ]
        self.source_issues = {id(p): empty_protocol_issues(p, self.laboratory, self.confirmations)
                              for p in self.protocols if not self.local_group[id(p)]}
        self.requests = {}
        self.type_numbers = {p.fields["type_number"].value for p in self.type_only_protocols}
        for protocol in self.registry_protocols:
            requests, number = self.registry_keys(protocol)
            self.requests.update(requests)
            self.type_numbers.add(number)

    @staticmethod
    def complete(protocol, records, *, ignore=None):
        return not protocol.error and bool(records) and len(records) == protocol.declared and all(
            all(issue == ignore for issue in result.blockers) for result in records
        )

    def registry_keys(self, protocol):
        when = iso_date(protocol.fields["verification_date"].value)
        return ({lookup_key(item, when.isoformat()): (item, when) for item in protocol.equipment},
                protocol.fields["type_number"].value)

    def retry_registry(self, protocol):
        requests, number = self.registry_keys(protocol) if protocol in self.registry_protocols else (
            {}, protocol.fields["type_number"].value
        )
        self.start_registry_checks(retry=(requests, {number}))

    def schedule_expiry(self):
        self.expiry_timer.stop()
        if self.online and not self.busy:
            expiry = self.registry.next_expiry(self.requests, self.type_numbers)
            if expiry is not None:
                self.expiry_timer.start(max(1, ceil((expiry - self.registry.clock()) * 1_000)))

    def start_registry_checks(self, *, retry=None, after_check=None):
        if self.busy:
            return
        self.check_inputs()
        requests, type_numbers = self.registry.pending(self.requests, self.type_numbers, retry)
        if not self.online or not (requests or type_numbers):
            self.recheck(inputs_checked=True)
            if after_check:
                self.continue_export(after_check)
            return
        self.expiry_timer.stop()
        self.after_registry_check = after_check
        self.lookup_thread = RegistryCheckThread(self.registry, requests, type_numbers, self)
        self.lookup_thread.assessed.connect(self.accept_registry_checks)
        self.lookup_thread.finished.connect(self.registry_checks_finished)
        self.set_notice("Проверяем сведения в ФИФ ОЕИ…")
        self.render()
        self.checked.emit()
        self.lookup_thread.start()

    def accept_registry_checks(self, result):
        lookups, types = result
        self.registry.remember(lookups, types)
        try:
            self.storage.save_registry_checks(c.entry for lookup in lookups.values()
                                              if lookup.state == "found" for c in lookup.candidates if c.eligible)
        except sqlite3.Error:
            logging.exception("Не удалось сохранить результаты сверки с реестром")

    def registry_checks_finished(self):
        self.lookup_thread.deleteLater()
        self.lookup_thread = None
        after_check, self.after_registry_check = self.after_registry_check, None
        self.recheck()
        if after_check:
            self.continue_export(after_check)

    def continue_export(self, save):
        if self.can_export:
            save()
        elif self.can_refresh_for_export():
            self.set_notice("Не удалось завершить проверку ФИФ ОЕИ. Проверьте подключение и повторите формирование XML.")
        else:
            self.set_notice("XML не сформирован. Откройте просмотр, чтобы проверить сведения ФИФ ОЕИ.")

    def recheck(self, *, inputs_checked=False):
        if not inputs_checked:
            self.check_inputs()
        if self.online:
            self.lookups, types = self.registry.snapshot(self.requests, self.type_numbers)
            self.type_titles = {number: result.title for number, result in types.items() if result.title}
            self.type_issues = {number: result.issue for number, result in types.items() if result.issue}
            self.retryable_types = {number for number, result in types.items() if result.retryable}
        type_issues = {}
        for protocol in self.registry_protocols + self.type_only_protocols:
            number = protocol.fields["type_number"].value
            if number not in self.type_titles:
                type_issues[number] = self.type_issues.get(number, f"Тип СИ {number} ещё не проверен в реестре.")
        online = check(self.registry_protocols, self.confirmations, self.entries, self.lookups,
                       laboratory=self.laboratory, require_public=True, type_issues=type_issues)
        self.group = self.local_group.copy()
        for protocol in self.registry_protocols:
            self.group[id(protocol)] = []
        for record in online:
            self.group[id(record.protocol)].append(record)
        for protocol in self.type_only_protocols:
            field = protocol.fields["type_number"]
            if issue := type_issues.get(field.value):
                issue += location(field)
                self.group[id(protocol)] = [
                    replace(record, shared_issues=(*record.shared_issues, issue), registry_issues=(issue,))
                    for record in self.local_group[id(protocol)]
                ]
        self.results = [record for protocol in self.protocols for record in self.group[id(protocol)]]
        self.render()
        self.schedule_expiry()
        self.checked.emit()

    @property
    def can_export(self):
        return not self.busy and bool(self.protocols) and all(
            self.complete(protocol, self.group[id(protocol)]) for protocol in self.protocols
        )

    def can_refresh_for_export(self):
        return (
            self.online and not self.busy and bool(self.protocols)
            and len(self.registry_protocols) == len(self.protocols)
            and not self.type_issues.keys() - self.retryable_types
            and not any(record.equipment_conflict for record in self.results)
            and any(self.registry.pending(self.requests, self.type_numbers, (self.requests, self.type_numbers)))
        )

    def state(self, protocol, records):
        if self.busy:
            return CHECKING
        if not self.complete(protocol, self.local_group[id(protocol)]):
            return ERROR
        if self.complete(protocol, records):
            return READY
        return REVIEW

    def update_marks(self):
        select_all = self.source.horizontalHeader().select_all
        select_all.setCheckState(self.source_model.all_check_state())
        select_all.setEnabled(bool(self.protocols) and not self.busy)
        self.remove_btn.setEnabled(bool(self.source_model.marked_paths) and not self.busy)

    def remove_protocols(self):
        if self.busy:
            return
        removed = self.source_model.marked_paths
        if not removed:
            return
        self.protocols = [p for p in self.protocols if p.path not in removed]
        self.recheck()

    def render(self):
        chosen = selected(self.source)
        rows = []
        for protocol in self.protocols:
            records = self.group[id(protocol)]
            rows.append((protocol, ("", protocol.number or protocol.path.name, protocol.reader_name,
                                    protocol.declared or len(records),
                                    self.state(protocol, records))))
        self.source_model.update(rows)
        if chosen:
            for row, (protocol, _) in enumerate(rows):
                if protocol.path == chosen.path:
                    self.source.selectRow(row)
                    break
        screened = {id(protocol) for protocol in self.registry_protocols}
        ready = sum(not record.blockers and id(record.protocol) in screened for record in self.results)
        total = sum(values[3] for _, values in rows)
        self.summary.setText(f"Протоколов {len(rows)}  ·  Приборов {total}  ·  Готово {ready}")
        self.empty.setVisible(not rows)
        self.source.setVisible(bool(rows))
        self.add_btn.setEnabled(not self.busy)
        self.settings_btn.setEnabled(not self.busy)
        self.preview_btn.setEnabled(bool(rows) and not self.busy)
        refreshable = self.can_refresh_for_export()
        self.export_btn.setEnabled(self.can_export or refreshable)
        self.export_btn.setToolTip(
            "Перед сохранением программа проверит сведения в ФИФ ОЕИ." if refreshable else
            "" if self.can_export else "Для выгрузки должны быть готовы все протоколы."
        )
        if not self.busy:
            self.set_notice("")

    def open_row(self, index):
        if index.column() != 0:
            self.open_protocol(self.source_model.rows[index.row()][0])

    def open_protocol(self, protocol):
        if not self.busy:
            dialog = ProtocolDialog(self, protocol)
            dialog.exec()
            dialog.deleteLater()

    def show_results(self):
        if self.protocols and not self.busy:
            self.open_protocol(selected(self.source))

    def laboratory_settings(self):
        if laboratory_settings(self, self.storage):
            self.recheck()

    def output_folder(self):
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        saved = self.storage.setting("output_folder")
        if saved:
            try:
                folder = Path(saved)
                if folder.is_dir():
                    return folder
            except OSError:
                pass
        return Path(documents) if documents and Path(documents).is_dir() else Path.home()

    def choose_xml_path(self, proposed):
        dialog = QFileDialog(self, "Сохранить как", str(proposed.parent))
        apply_dialog_icon(dialog)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter("Файлы XML (*.xml)")
        dialog.setDefaultSuffix("xml")
        dialog.selectFile(proposed.name)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                return Path(dialog.selectedFiles()[0])
            return None
        finally:
            dialog.deleteLater()

    def export_xml(self):
        if self.busy:
            return
        self.recheck()
        if self.can_export:
            self.save_xml()
        elif self.can_refresh_for_export():
            self.start_registry_checks(retry=(self.requests, self.type_numbers), after_check=self.save_xml)

    def save_xml(self, destination=None, *, alternate_folder=None, refresh_after_dialog=True):
        self.expiry_timer.stop()
        try:
            self.write_xml(destination, alternate_folder=alternate_folder, refresh_after_dialog=refresh_after_dialog)
        finally:
            self.schedule_expiry()

    def xml_stem(self):
        name = self.protocols[0].number if len(self.protocols) == 1 else ""
        name = name or date.today().isoformat()
        return APP_NAME + "_" + re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")[:100]

    def choose_destination(self, stem, packet_count):
        folder = self.output_folder()
        if packet_count == 1:
            path = self.choose_xml_path(folder / f"{stem}.xml")
            return ([path], path.stat() if path.exists() else None) if path is not None else None
        chosen = QFileDialog.getExistingDirectory(self, f"Папка для XML ({packet_count} файлов)", str(folder))
        return (split_paths(Path(chosen), stem, packet_count), None) if chosen else None

    def defer_stale_export(self, resume, refresh_after_dialog):
        if not self.online or not any(self.registry.pending(self.requests, self.type_numbers)):
            return False
        if refresh_after_dialog:
            self.start_registry_checks(after_check=resume)
        else:
            self.recheck()
            self.set_notice("Срок проверки сведений ФИФ ОЕИ истёк. Нажмите «Сформировать XML» ещё раз.")
        return True

    def write_in_folder(self, packets, stem, folder, *, alternate_folder, refresh_after_dialog):
        if not folder.is_absolute():
            raise OSError("Не указана папка автоматического сохранения.")
        for attempt in range(3):
            paths = automatic_paths(folder, stem, len(packets))
            if self.defer_stale_export(
                lambda: self.save_xml(alternate_folder=alternate_folder, refresh_after_dialog=False),
                refresh_after_dialog,
            ):
                return None
            try:
                return export(packets, self.schema, paths, self.storage, f"Сеанс Windows: {getuser()}")
            except FileExistsError:
                if attempt == 2:
                    raise

    def save_automatically(self, packets, stem, configured_folder, alternate_folder, refresh_after_dialog):
        folder = alternate_folder if alternate_folder is not None else Path(configured_folder)
        try:
            return self.write_in_folder(packets, stem, folder, alternate_folder=alternate_folder,
                                        refresh_after_dialog=refresh_after_dialog)
        except OSError:
            if alternate_folder is not None:
                raise
            logging.warning("Не удалось сохранить XML в настроенную папку", exc_info=True)
        if message(self, "Сохранение XML",
                   f"Не удалось сохранить XML в настроенную папку. Проверьте доступ и свободное место.\n"
                   f"{configured_folder}\n\nВыбрать другую папку для этой выгрузки?",
                   ask=True, confirm_text="Выбрать папку…"):
            chosen = QFileDialog.getExistingDirectory(self, "Папка для этой выгрузки", str(self.output_folder()))
            if chosen:
                folder = Path(chosen)
                return self.write_in_folder(packets, stem, folder, alternate_folder=folder,
                                            refresh_after_dialog=refresh_after_dialog)
        self.set_notice("XML не сохранён.")
        return None

    def write_xml(self, destination, *, alternate_folder, refresh_after_dialog):
        try:
            packets = format_packets(self.results, self.schema)
            stem = self.xml_stem()
            automatic, configured_folder = self.storage.xml_output()
            if automatic:
                files = self.save_automatically(packets, stem, configured_folder, alternate_folder, refresh_after_dialog)
            else:
                if destination is None or len(destination[0]) != len(packets):
                    destination = self.choose_destination(stem, len(packets))
                if destination is None or self.defer_stale_export(
                    lambda: self.save_xml(destination, refresh_after_dialog=False), refresh_after_dialog
                ):
                    return
                paths, overwrite = destination
                files = export(packets, self.schema, paths, self.storage, f"Сеанс Windows: {getuser()}",
                               overwrite=overwrite)
        except ValueError as exc:
            logging.exception("Ошибка проверки формата XML")
            message(self, "Проверка формата XML", str(exc), error=True)
            return
        except (OSError, sqlite3.Error):
            logging.exception("Ошибка формирования XML")
            message(self, "Сохранение XML", "Не удалось сохранить XML. Проверьте права доступа и свободное место.", error=True)
            return
        if not files:
            return
        if not automatic:
            try:
                self.storage.set_setting("output_folder", str(files[0].parent))
            except sqlite3.Error:
                logging.exception("Не удалось запомнить папку ручного сохранения")
        suffix = f" · файлов {len(files)}" if len(files) > 1 else ""
        location = files[0].parent if len(files) > 1 else files[0]
        self.set_notice(f"XML сохранён: {len(self.results)} записей{suffix}\n{location}")
