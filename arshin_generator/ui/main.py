from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from getpass import getuser
import logging
from pathlib import Path
import re
import sqlite3

from PySide6.QtCore import QItemSelectionModel, QStandardPaths, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QMainWindow, QMenu, QToolButton,
    QVBoxLayout, QWidget,
)

from ..arshin import export, format_packets, split_paths
from ..confirmations import get_confirmation, protocol_key, with_guards
from ..public_registry import PublicDataError, official_type_title
from ..readers import EXTENSIONS, read_protocol
from ..registry import EquipmentLookup, iso_date, lookup_equipment, lookup_key
from ..validation import check
from .common import Form, button, fit_window, label, message, table
from .dialogs import HistoryDialog, XmlDialog
from .protocols import ProtocolDialog
from .workflow import ConnectionDialog, ExpiryRules, laboratory_settings


class ImportThread(QThread):
    imported = Signal(object)

    def __init__(self, paths, parent):
        super().__init__(parent)
        self.paths = paths

    def run(self):
        self.imported.emit([read_protocol(path) for path in self.paths])


class RegistryCheckThread(QThread):
    assessed = Signal(object)

    def __init__(self, requests, type_numbers, parent):
        super().__init__(parent)
        self.requests = requests
        self.type_numbers = type_numbers

    def run(self):
        no_connection = EquipmentLookup("unavailable", "Не удалось подключиться к реестру. Повторите проверку")

        def verify(request):
            if self.isInterruptionRequested():
                return EquipmentLookup("response", "Поиск остановлен. Повторите проверку")
            item, day = request
            try:
                result = lookup_equipment(item, day)
            except Exception:
                logging.exception("Ошибка проверки публичной карточки")
                return no_connection
            return result

        def find_title(number):
            if self.isInterruptionRequested():
                return "", "Поиск типа СИ остановлен. Повторите проверку."
            try:
                title = official_type_title(number)
            except PublicDataError:
                return "", f"Не удалось проверить тип СИ {number} в государственном реестре."
            return title, "" if title else f"В реестре не найден точный тип СИ {number}. Проверьте номер в Excel."

        with ThreadPoolExecutor(max_workers=2) as executor:
            types = {number: executor.submit(find_title, number) for number in self.type_numbers}
            verified = dict(zip(self.requests, executor.map(verify, self.requests.values())))
            self.assessed.emit((verified, {number: request.result() for number, request in types.items()}))


class MainWindow(QMainWindow):
    checked = Signal()

    def __init__(self, storage, schema, *, online=True):
        super().__init__()
        self.storage = storage
        self.schema = schema
        self.online = online
        self.protocols = []
        self.results = []
        self.thread = None
        self.lookup_thread = None
        self.lookups = {}
        self.type_titles = {}
        self.type_issues = {}
        self.setWindowTitle("Аршин · Генератор XML")
        self.setMinimumSize(680, 440)
        fit_window(self, 940, 620)
        self.setAcceptDrops(True)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(26, 22, 26, 19)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(4)
        title.addWidget(label("Протоколы поверки", "workTitle"))
        self.summary = label("")
        title.addWidget(self.summary)
        header.addLayout(title, 1)
        self.settings_btn = button("Настройки")
        menu = QMenu(self.settings_btn)
        menu.addAction("Реквизиты лаборатории", self.laboratory_settings)
        service = menu.addMenu("Служебные инструменты")
        service.addAction("Правила срока поверки", self.expiry_rules)
        service.addAction("Исключение по сроку поверки…", self.expiry_exception)
        service.addAction("История и резервная копия", self.history)
        service.addAction("Открыть файл XML…", self.pick_xml)
        service.addAction("Проверить подключение", self.connection)
        self.settings_btn.setMenu(menu)
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

        self.empty = QFrame(objectName="emptyQueue")
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(26, 26, 26, 26)
        empty_layout.addStretch()
        for item in (label("Выберите протоколы Excel", "heading"),
                     label("Можно перетащить файлы сюда. Проверка начнётся автоматически.")):
            item.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(item)
        empty_layout.addStretch()
        layout.addWidget(self.empty, 1)

        self.source, self.source_model = table(("Протокол", "Приборов", "Состояние"), (214, 85, 235))
        self.source.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        sizes = self.source.horizontalHeader()
        sizes.setStretchLastSection(False)
        sizes.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        sizes.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.source.verticalHeader().setDefaultSectionSize(46)
        layout.addWidget(self.source, 1)
        self.source.doubleClicked.connect(lambda index: self.open_protocol(self.source_model.rows[index.row()][0]))
        self.source.selectionModel().selectionChanged.connect(self.update_remove_button)

        footer = QHBoxLayout()
        self.notice = label("")
        footer.addWidget(self.notice, 1)
        layout.addLayout(footer)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.pick_files)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, activated=self.remove_protocols)
        self.recheck()

    @property
    def busy(self):
        return self.thread is not None or self.lookup_thread is not None

    def closeEvent(self, event):
        if self.busy:
            self.notice.setText("Дождитесь окончания проверки.")
            if self.lookup_thread:
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
            self.notice.setText("В выбранной папке нет книг Excel.")
            return
        self.storage.set_setting("input_folder", str(files[0].parent))
        self.notice.setText("Читаем протоколы…")
        self.thread = ImportThread(list(dict.fromkeys(path.resolve() for path in files)), self)
        self.thread.imported.connect(self.append_protocols)
        self.thread.finished.connect(self.import_finished)
        self.render()
        self.thread.start()

    def append_protocols(self, incoming):
        for protocol in incoming:
            if not protocol.error:
                self.storage.migrate_confirmation(protocol)
            self.storage.audit("Чтение протокола", {
                "path": str(protocol.path), "digest": protocol.digest, "reader": protocol.reader,
                "number": protocol.number, "error": protocol.error,
            })
            existing = next((p for p in self.protocols if p.path == protocol.path), None)
            if any(p.digest == protocol.digest and protocol.digest and p is not existing for p in self.protocols):
                continue
            if existing:
                self.protocols[self.protocols.index(existing)] = protocol
            else:
                self.protocols.append(protocol)

    def import_finished(self):
        self.thread.deleteLater()
        self.thread = None
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

    @staticmethod
    def complete(protocol, records):
        return not protocol.error and bool(records) and len(records) == protocol.declared and all(
            not result.blockers for result in records
        )

    def start_registry_checks(self):
        if self.busy:
            return
        self.check_inputs()
        requests = {}
        type_numbers = set()
        for protocol in self.registry_protocols:
            type_numbers.add(protocol.fields["type_number"].value)
            when = iso_date(protocol.fields["verification_date"].value)
            for item in protocol.equipment:
                requests[lookup_key(item, when.isoformat())] = item, when
        if not self.online or not type_numbers:
            self.notice.clear()
            self.recheck()
            return
        self.lookup_thread = RegistryCheckThread(requests, type_numbers, self)
        self.lookup_thread.assessed.connect(self.accept_registry_checks)
        self.lookup_thread.finished.connect(self.registry_checks_finished)
        self.notice.setText("Проверяем сведения в государственном реестре…")
        self.render()
        self.checked.emit()
        self.lookup_thread.start()

    def accept_registry_checks(self, result):
        self.lookups, types = result
        self.type_titles = {number: title for number, (title, _) in types.items() if title}
        self.type_issues = {number: issue for number, (_, issue) in types.items() if issue}
        try:
            self.storage.save_registry_checks(c.entry for lookup in self.lookups.values()
                                              if lookup.state == "found" for c in lookup.candidates if c.eligible)
        except sqlite3.Error:
            logging.exception("Не удалось сохранить результаты сверки с реестром")

    def registry_checks_finished(self):
        self.lookup_thread.deleteLater()
        self.lookup_thread = None
        self.notice.clear()
        self.recheck()

    def refresh_after_edit(self):
        self.start_registry_checks()

    def recheck(self):
        self.check_inputs()
        type_issues = {}
        for protocol in self.registry_protocols:
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
        self.results = [record for protocol in self.protocols for record in self.group[id(protocol)]]
        self.render()
        self.checked.emit()

    @property
    def can_export(self):
        return not self.busy and bool(self.protocols) and all(
            self.complete(protocol, self.group[id(protocol)]) for protocol in self.protocols
        )

    def state(self, protocol, records):
        if self.busy:
            return "Проверяем…"
        if self.complete(protocol, records):
            return "Готов к выгрузке"
        if records:
            issue = next((issue for record in records for issue in record.blockers), "")
            issue = issue.removeprefix("Подтверждение: ").strip()
            if issue:
                return issue[0].upper() + issue[1:]
        return protocol.error or next(iter(protocol.issues), "") or protocol.serial_issue or "Нет отдельных СИ"

    def selected_protocols(self):
        return [self.source_model.rows[index.row()][0] for index in self.source.selectionModel().selectedRows()]

    def update_remove_button(self, *_):
        self.remove_btn.setEnabled(bool(self.selected_protocols()) and not self.busy)

    def remove_protocols(self):
        if self.busy:
            return
        removed = {id(p) for p in self.selected_protocols()}
        if not removed:
            return
        checked = {id(p) for p in self.registry_protocols}
        self.protocols = [p for p in self.protocols if id(p) not in removed]
        self.recheck()
        if any(id(p) not in checked for p in self.registry_protocols):
            self.start_registry_checks()

    def render(self):
        chosen = {id(p) for p in self.selected_protocols()}
        rows = []
        for protocol in self.protocols:
            records = self.group[id(protocol)]
            rows.append((protocol, (protocol.number or protocol.path.name, protocol.declared or len(records),
                                    self.state(protocol, records))))
        self.source_model.update(rows)
        selection = self.source.selectionModel()
        for row, (protocol, _) in enumerate(rows):
            if id(protocol) in chosen:
                selection.select(self.source_model.index(row, 0), QItemSelectionModel.SelectionFlag.Select |
                                 QItemSelectionModel.SelectionFlag.Rows)
        screened = {id(protocol) for protocol in self.registry_protocols}
        ready = sum(not record.blockers and id(record.protocol) in screened for record in self.results)
        total = sum(values[1] for _, values in rows)
        self.summary.setText(f"Протоколов {len(rows)}    ·    Приборов {total}    ·    Готово {ready}")
        self.empty.setVisible(not rows)
        self.source.setVisible(bool(rows))
        self.add_btn.setEnabled(not self.busy)
        self.settings_btn.setEnabled(not self.busy)
        self.update_remove_button()
        self.preview_btn.setEnabled(bool(rows) and not self.busy)
        self.export_btn.setEnabled(self.can_export)
        self.export_btn.setToolTip("" if self.can_export else "Для выгрузки должны быть готовы все протоколы.")
        if not self.busy:
            if self.can_export:
                self.notice.setText(f"В пакет войдёт записей: {ready}")
            elif rows:
                blocked = sum(not self.complete(p, self.group[id(p)]) for p, _ in rows)
                problem_records = sum(sum(bool(r.blockers) for r in self.group[id(p)]) +
                                      max(0, values[1] - len(self.group[id(p)])) for p, values in rows)
                self.notice.setText(f"Требуют внимания: протоколов {blocked}, приборов {problem_records}. "
                                    "Просмотрите сведения.")
            else:
                self.notice.clear()

    def open_protocol(self, protocol):
        if not self.busy:
            dialog = ProtocolDialog(self, protocol)
            dialog.exec()
            dialog.deleteLater()

    def show_results(self):
        if self.protocols and not self.busy:
            chosen = self.selected_protocols()
            self.open_protocol(chosen[0] if chosen else None)

    def connection(self):
        ConnectionDialog(self, self.open_log).exec()

    def laboratory_settings(self):
        laboratory_settings(self, self.storage)
        self.refresh_after_edit()

    def expiry_rules(self):
        ExpiryRules(self, self.storage).exec()
        self.refresh_after_edit()

    def expiry_exception(self):
        selected = self.selected_protocols() or self.protocols
        if self.busy or len(selected) != 1 or not selected[0].instruments:
            message(self, "Исключение по сроку", "Выберите один заполненный протокол.")
            return
        protocol = selected[0]
        day = iso_date(protocol.fields["verification_date"].value)
        if not day:
            message(self, "Исключение по сроку", "Сначала укажите дату поверки в Excel.")
            return
        previous = get_confirmation(self.storage.confirmations(), "expiry_exception", protocol)
        form = Form(self, "Исключение по сроку поверки",
                    f"Протокол {protocol.number or protocol.path.name}. Введите дату и ссылку на документ, "
                    "который устанавливает исключение.", size=(535, 330), save="Сохранить")
        form.field("valid_date", "Дата окончания", "date", previous[0].get("valid_date") if previous else None)
        form.field("basis", "Документ и пункт", value=previous[1] if previous else "")

        def validate(values):
            end = iso_date(values["valid_date"])
            if not end or end < day or not values["basis"]:
                raise ValueError("Укажите основание и дату окончания не ранее даты поверки.")

        form.validate = validate
        if form.exec():
            values = form.values()
            fields = with_guards(protocol, {"valid_date": values["valid_date"]})
            self.storage.save_confirmation("expiry_exception", protocol_key(protocol), fields, values["basis"], getuser())
            self.refresh_after_edit()

    def output_folder(self):
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        saved = self.storage.setting("output_folder")
        if saved and Path(saved).is_dir():
            return Path(saved)
        return Path(documents) if documents and Path(documents).is_dir() else Path.home()

    def history(self):
        HistoryDialog(self, self.storage, self.schema).exec()

    def open_log(self):
        path = self.storage.path.parent / "diagnostics.log"
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            message(self, "Диагностический журнал", "В журнале пока нет записей.")

    def pick_xml(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Открыть сформированный XML",
                                                str(self.output_folder()), "Файлы XML (*.xml)")
        if files:
            paths = [Path(file) for file in files]
            local = {path.resolve().parent for path in paths}
            context = {}
            for entry in self.storage.history():
                if Path(entry["directory"]).resolve() in local:
                    context.update(self.storage.export_context(entry["id"]))
            XmlDialog(self, paths, self.schema, context).exec()

    def choose_xml_path(self, proposed):
        dialog = QFileDialog(self, "Сохранить как", str(proposed.parent))
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
        if not self.can_export:
            return
        previous = self.storage.seen([r.identity for r in self.results])
        if previous and not message(self, "Повторная выгрузка",
                f"Ранее сформировано результатов: {len(previous)}. При повторной загрузке могут появиться дубликаты. "
                "Сформировать XML ещё раз?", ask=True):
            return
        try:
            packets = format_packets(self.results, self.schema)
            name = self.protocols[0].number if len(self.protocols) == 1 else ""
            name = name or date.today().isoformat()
            stem = "Аршин_" + re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")[:100]
            folder = self.output_folder()
            overwrite = None
            if len(packets) == 1:
                path = self.choose_xml_path(folder / f"{stem}.xml")
                if path is None:
                    return
                paths = [path]
                if path.exists():
                    overwrite = path.stat()
            else:
                chosen = QFileDialog.getExistingDirectory(self, f"Папка для XML ({len(packets)} файлов)", str(folder))
                if not chosen:
                    return
                paths = split_paths(Path(chosen), stem, len(packets))
            files = export(packets, self.schema, paths, self.storage, f"Сеанс Windows: {getuser()}",
                           overwrite=overwrite)
        except (ValueError, OSError, sqlite3.Error) as exc:
            logging.exception("Ошибка формирования XML")
            message(self, "Выгрузка", str(exc) if isinstance(exc, ValueError) else
                    "Не удалось сохранить XML. Проверьте права доступа и свободное место.", error=True)
            return
        try:
            self.storage.set_setting("output_folder", str(files[0].parent))
        except sqlite3.Error:
            logging.exception("Не удалось запомнить папку выгрузки")
        suffix = f" · файлов {len(files)}" if len(files) > 1 else ""
        self.notice.setText(f"XML сохранён: {len(self.results)} записей{suffix}")
