from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from getpass import getuser
import logging
from pathlib import Path
import sqlite3

from PySide6.QtCore import QStandardPaths, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QMainWindow, QMenu, QToolButton, QVBoxLayout, QWidget,
)

from ..arshin import export
from ..public_registry import PublicDataError, official_type_title
from ..readers import EXTENSIONS, read_protocol
from ..registry import EquipmentLookup, iso_date, lookup_equipment, lookup_key, resolve_many
from ..validation import check
from .common import button, fit_window, label, message, selected, table
from .dialogs import HistoryDialog, RegistryDialog, XmlDialog
from .workflow import ConnectionDialog, ExpiryRules, PreviewDialog, RepairDialog, laboratory_settings


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
                return ""
            try:
                return official_type_title(number)
            except PublicDataError:
                return ""

        with ThreadPoolExecutor(max_workers=2) as executor:
            types = {number: executor.submit(find_title, number) for number in self.type_numbers}
            verified = dict(zip(self.requests, executor.map(verify, self.requests.values())))
            self.assessed.emit((verified, {number: request.result() for number, request in types.items()}))


class MainWindow(QMainWindow):
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
        settings = button("Настройки")
        menu = QMenu(settings)
        menu.addAction("Реквизиты лаборатории", self.laboratory_settings)
        menu.addAction("Правила срока поверки", self.expiry_rules)
        menu.addAction("Папка для XML…", self.pick_output_folder)
        menu.addSeparator()
        menu.addAction("Проверить подключение", self.connection)
        menu.addAction("Справочник средств поверки", self.registry)
        menu.addAction("История выгрузок", self.history)
        menu.addAction("Открыть файл XML…", self.pick_xml)
        menu.addAction("Диагностический журнал", self.open_log)
        menu.addAction("Папка данных", self.open_data)
        settings.setMenu(menu)
        header.addWidget(settings, alignment=Qt.AlignmentFlag.AlignTop)
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

        self.source, self.source_model = table(("Протокол", "Приборов", "Состояние", "Действие"), (214, 85, 235))
        sizes = self.source.horizontalHeader()
        sizes.setStretchLastSection(False)
        sizes.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        sizes.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.source.setColumnWidth(3, 118)
        self.source.verticalHeader().setDefaultSectionSize(46)
        layout.addWidget(self.source, 1)
        self.source.doubleClicked.connect(lambda index: self.open_protocol(self.source_model.rows[index.row()][0]))

        footer = QHBoxLayout()
        self.notice = label("")
        footer.addWidget(self.notice, 1)
        self.retry_btn = button("Повторить поиск", self.start_registry_checks)
        footer.addWidget(self.retry_btn)
        layout.addLayout(footer)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.pick_files)
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
        self.thread = ImportThread(list(dict.fromkeys(files)), self)
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

    def start_registry_checks(self):
        if self.busy:
            return
        requests = {}
        type_numbers = set()
        for protocol in self.protocols:
            if protocol.error or not protocol.instruments:
                continue
            number = protocol.fields["type_number"].value
            if isinstance(number, str) and number and number not in self.type_titles:
                type_numbers.add(number)
            when = iso_date(protocol.fields["verification_date"].value)
            if when:
                for item in protocol.equipment:
                    requests[lookup_key(item, when.isoformat())] = item, when
        self.lookups = {}
        if not self.online or not (requests or type_numbers):
            self.notice.clear()
            self.recheck()
            return
        self.lookup_thread = RegistryCheckThread(requests, type_numbers, self)
        self.lookup_thread.assessed.connect(self.accept_registry_checks)
        self.lookup_thread.finished.connect(self.registry_checks_finished)
        self.notice.setText("Проверяем сведения в реестре…")
        self.recheck()
        self.lookup_thread.start()

    def accept_registry_checks(self, result):
        self.lookups, titles = result
        self.type_titles.update({number: title for number, title in titles.items() if title})

    def registry_checks_finished(self):
        self.lookup_thread.deleteLater()
        self.lookup_thread = None
        self.notice.clear()
        self.recheck()

    def recheck(self):
        self.entries = self.storage.entries()
        self.results = check(self.protocols, self.storage.confirmations(), self.entries, self.lookups,
                             laboratory=self.storage.laboratory())
        self.group = defaultdict(list)
        for item in self.results:
            self.group[id(item.protocol)].append(item)
        self.render()

    def has_connection_issue(self, protocol):
        if protocol.error:
            return False
        when = iso_date(protocol.fields["verification_date"].value)
        if not when:
            return False
        for item in protocol.equipment:
            live = self.lookups.get(lookup_key(item, when.isoformat()))
            if live and live.state == "unavailable" and not resolve_many(item, self.entries, when, live)[0]:
                return True
        return False

    def state(self, protocol, records):
        if self.busy:
            return "Проверяем…"
        if protocol.error or protocol.issues or not records or any(
            r.instrument.problem or any(not reading.complete for reading in r.instrument.elements) for r in records
        ):
            return "Ошибка в протоколе"
        ready = sum(not r.blockers for r in records)
        if ready == len(records):
            return "Готов к выгрузке"
        if self.has_connection_issue(protocol):
            return "Нет связи с реестром"
        return f"Готово {ready} из {len(records)}" if ready else "Требуется уточнение"

    def render(self):
        rows = []
        for p in self.protocols:
            records = self.group[id(p)]
            state = self.state(p, records)
            rows.append((p, (p.number or p.path.name, p.declared or len(records), state, "")))
        self.source_model.update(rows)
        for row, (protocol, values) in enumerate(rows):
            action = button("Открыть" if values[2] == "Готов к выгрузке" else "Исправить",
                            lambda checked=False, p=protocol: self.open_protocol(p))
            action.setObjectName("rowAction")
            action.setEnabled(not self.busy)
            self.source.setIndexWidget(self.source_model.index(row, 3), action)
        ready = sum(not record.blockers for record in self.results)
        self.summary.setText(f"Протоколов {len(rows)}    ·    Приборов {len(self.results)}    ·    Готово {ready}")
        self.empty.setVisible(not rows)
        self.source.setVisible(bool(rows))
        self.add_btn.setEnabled(not self.busy)
        self.preview_btn.setEnabled(bool(self.results) and not self.busy)
        self.export_btn.setEnabled(bool(ready) and not self.busy)
        self.export_btn.setToolTip("Куда сохранять: " + str(self.output_folder()) if ready else
                                   "Готовые записи появятся после устранения замечаний.")
        retry = any(self.has_connection_issue(p) for p in self.protocols)
        self.retry_btn.setVisible(not self.busy and retry)
        if retry and not self.busy:
            self.notice.setText("Не удалось подключиться к реестру.")
        elif rows and not self.busy:
            if not ready:
                self.notice.setText("Откройте «Исправить», чтобы увидеть препятствия для выгрузки.")
            else:
                self.notice.setText(f"В пакет войдёт записей: {ready}")

    def open_protocol(self, protocol):
        if not self.busy:
            RepairDialog(self, protocol).exec()

    def show_results(self):
        if self.results and not self.busy:
            PreviewDialog(self, self.results, type_titles=self.type_titles).exec()

    def connection(self):
        ConnectionDialog(self, self.open_log).exec()

    def laboratory_settings(self):
        laboratory_settings(self, self.storage)
        self.recheck()

    def expiry_rules(self):
        ExpiryRules(self, self.storage).exec()
        self.recheck()

    def output_folder(self):
        saved = self.storage.setting("output_folder")
        if saved:
            return Path(saved)
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        return (Path(documents) if documents else Path.home()) / "Аршин"

    def pick_output_folder(self):
        current = self.output_folder()
        folder = QFileDialog.getExistingDirectory(self, "Папка для XML", str(current if current.is_dir() else current.parent))
        if folder:
            self.storage.set_setting("output_folder", folder)
            self.render()

    def registry(self):
        protocol = selected(self.source)
        if protocol is None and len(self.protocols) == 1:
            protocol = self.protocols[0]
        RegistryDialog(self, self.storage, protocol.equipment if protocol else ()).exec()
        self.recheck()

    def history(self):
        HistoryDialog(self, self.storage, self.schema).exec()

    def open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.storage.path.parent)))

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

    def source_errors(self, records):
        present = {id(r.protocol) for r in records}
        return [{"protocol": p.number or p.path.name, "serial": "", "issues":
                 [p.error] if p.error else p.issues + [p.serial_issue or "Нет отдельных СИ."]}
                for p in self.protocols if id(p) not in present]

    def export_xml(self):
        if self.busy:
            return
        self.recheck()
        ready = [result for result in self.results if not result.blockers]
        if not ready:
            return
        excluded = [{"protocol": r.protocol.number or r.protocol.path.name, "serial": r.instrument.serial,
                     "issues": list(r.blockers)} for r in self.results if r.blockers]
        excluded.extend(self.source_errors(self.results))
        previous = self.storage.seen([r.identity for r in ready])
        repeated = [f"{r.protocol.number} / {r.instrument.serial}" for r in ready if r.identity in previous]
        if (excluded or repeated) and not self.confirm_export_scope(ready, excluded, repeated):
            return
        evidence = ("Подтверждён состав выгрузки" if excluded or repeated else "Все результаты проверены")
        confirmer = f"Сеанс Windows: {getuser()}"
        folder = self.output_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            files = export(ready, self.schema, folder, self.storage, excluded, f"{confirmer}: {evidence}")
        except (ValueError, OSError, sqlite3.Error) as exc:
            logging.exception("Ошибка формирования XML")
            message(self, "Выгрузка", str(exc) if isinstance(exc, ValueError) else
                    "Не удалось сохранить файл. Укажите доступную папку в меню «Настройки → Папка для XML».",
                    error=True)
            return
        self.show_saved(files, ready)

    def show_saved(self, files, records):
        dialog = QDialog(self)
        title = "Выгрузка готова"
        dialog.setWindowTitle(title)
        fit_window(dialog, 700, 300)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(20, 18, 20, 18)
        box.addWidget(label(title, "heading"))
        box.addWidget(label(f"Записей {len(records)}    ·    Файлов {len(files)}"))
        box.addWidget(label("При загрузке файла в «Аршин» записи автоматически включаются в заявку на публикацию."))
        names = label("\n".join(str(path) for path in files))
        names.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(names, 1)
        actions = QHBoxLayout()
        actions.addWidget(button("Открыть папку", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(files[0].parent))), primary=True))
        context = {(r.instrument.serial, r.value("verification_date"), r.value("type_number")): r.protocol.number
                   for r in records}
        actions.addWidget(button("Просмотреть файл", lambda: XmlDialog(dialog, files, self.schema, context).exec()))
        actions.addStretch()
        actions.addWidget(button("Закрыть", dialog.accept))
        box.addLayout(actions)
        dialog.exec()

    def confirm_export_scope(self, ready, excluded, repeated):
        detail = [f"В пакет войдёт записей: {len(ready)}."]
        if excluded:
            grouped = defaultdict(list)
            for entry in excluded:
                grouped[entry["protocol"]].append(entry["serial"])
            detail.append("Записи с замечаниями останутся в списке протоколов:")
            detail.extend(f"• {protocol} — {len(serials)}" if all(serials) else f"• {protocol}"
                          for protocol, serials in grouped.items())
        if repeated:
            detail.append(f"Ранее выгружено записей: {len(repeated)}. Они войдут в новый пакет.")
        return message(self, "Состав выгрузки", "\n".join(detail), ask=True)
