from collections import Counter
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMenu, QMessageBox, QProgressBar, QToolButton, QWidget

from ..pdf_export import PdfResult, check_folder, create_pdfs, plan_pdf
from ..readers import EXTENSIONS
from .common import apply_dialog_icon, button, label, message


class PdfThread(QThread):
    progress = Signal(object, int, int)
    completed = Signal(object)

    def __init__(self, parent, jobs, replace):
        super().__init__(parent)
        self.jobs = jobs
        self.replace = replace

    def run(self):
        try:
            results = create_pdfs(self.jobs, replace_existing=self.replace, progress=self.progress.emit)
        except Exception:
            logging.exception("Сбой пакетного экспорта PDF")
            results = [PdfResult(job.source, "error", "Экспорт PDF прерван. Подробности записаны в журнал.")
                       for job in self.jobs]
        self.completed.emit(results)


class PdfControls(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.thread = None
        self.folder = None
        self.results = []
        self.action = QToolButton()
        self.action.setText("Создать PDF")
        self.action.setObjectName("pdfAction")
        self.action.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.action.clicked.connect(lambda: self.start())
        menu = QMenu(self.action)
        menu.addAction("Выбрать файлы…", lambda: self.start(self.window.choose_input_files()))
        menu.addAction("Выбрать папку с протоколами…", self.source_folder)
        self.action.setMenu(menu)

        self.footer = QWidget()
        row = QHBoxLayout(self.footer)
        row.setContentsMargins(0, 0, 0, 0)
        self.status = label("")
        row.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(110)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Выполнение экспорта PDF")
        row.addWidget(self.progress)
        self.details = button("Замечания…", self.show_details)
        self.open = button("Открыть папку", self.open_folder)
        row.addWidget(self.details)
        row.addWidget(self.open)
        self.footer.hide()

    def source_folder(self):
        folder = self.window.choose_input_folder()
        if folder:
            try:
                files = sorted(path for path in folder.iterdir() if path.is_file() and
                               path.suffix.casefold() in EXTENSIONS and not path.name.startswith("~$"))
            except OSError:
                message(self.window, "Выбор протоколов", "Не удалось открыть выбранную папку.", error=True)
                return
            if not files:
                message(self.window, "Выбор протоколов", "В папке нет поддерживаемых книг Excel.")
            self.start(files)

    def destination(self):
        configured = self.window.storage.setting("pdf_folder")
        if configured:
            try:
                check_folder(Path(configured))
                return Path(configured)
            except OSError:
                if not message(self.window, "Папка для PDF", "Сохранённая папка для PDF недоступна.",
                               ask=True, confirm_text="Выбрать другую папку…"):
                    return None
        chosen = QFileDialog.getExistingDirectory(self.window, "Папка для сохранения PDF", str(self.window.output_folder()))
        if not chosen:
            return None
        folder = Path(chosen)
        try:
            check_folder(folder)
        except OSError:
            message(self.window, "Папка для PDF", "Не удалось записать файл в выбранную папку. Выберите доступную папку.",
                    error=True)
            return None
        return folder

    def collision_decision(self, jobs):
        existing = sum(job.existing is not None and not job.duplicate for job in jobs)
        duplicates = sum(job.duplicate for job in jobs)
        if not (existing or duplicates):
            return False
        dialog = QMessageBox(self.window)
        apply_dialog_icon(dialog)
        dialog.setWindowTitle("Совпадающие имена PDF")
        text = f"PDF с такими именами уже есть в папке: {existing}." if existing else ""
        if duplicates:
            text = (text + "\n" if text else "") + (
                f"Среди исходников есть совпадающие имена ({duplicates}). Для каждого имени берётся первый файл в списке.")
        dialog.setText(text)
        replace = dialog.addButton("Заменить существующие", QMessageBox.ButtonRole.AcceptRole) if existing else None
        skip = dialog.addButton("Пропустить совпадающие" if existing else "Продолжить", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(skip)
        dialog.exec()
        pressed = dialog.clickedButton()
        if pressed is skip:
            return False
        return True if replace is not None and pressed is replace else None

    def start(self, paths=None):
        if self.thread is not None:
            return
        if paths is None:
            paths = [protocol.path for protocol in self.window.protocols] or self.window.choose_input_files()
        if not paths or (folder := self.destination()) is None:
            return
        try:
            jobs = plan_pdf(paths, folder)
        except OSError:
            message(self.window, "Папка для PDF", "Не удалось проверить имена файлов в папке назначения.", error=True)
            return
        replace = self.collision_decision(jobs)
        if replace is None:
            return
        if str(folder) != self.window.storage.setting("pdf_folder") and message(
            self.window, "Папка для PDF", "Запомнить эту папку для следующих выгрузок PDF?", ask=True,
            confirm_text="Запомнить папку", cancel_text="Только сейчас",
        ):
            self.window.storage.set_setting("pdf_folder", str(folder))
        self.folder = folder
        self.results = []
        self.details.hide()
        self.open.hide()
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.show()
        self.footer.show()
        self.status.setText("Подготавливаем экспорт PDF…")
        self.action.setEnabled(False)
        self.thread = PdfThread(self, jobs, replace)
        self.thread.progress.connect(self.show_progress)
        self.thread.completed.connect(self.record_results)
        self.thread.finished.connect(self.finished)
        self.thread.start()

    def show_progress(self, source, index, total):
        self.status.setText(f"Создаётся PDF: {source.name} ({index} из {total})")
        self.progress.setValue(index - 1)

    def record_results(self, results):
        self.results = results

    def finished(self):
        self.thread.deleteLater()
        self.thread = None
        counts = Counter(item.status for item in self.results)
        self.status.setText(f"Создано PDF: {counts['created']} · Пропущено: {counts['skipped']} · Ошибок: {counts['error']}")
        self.progress.hide()
        self.details.setVisible(bool(counts["error"] or counts["skipped"]))
        self.open.show()
        self.action.setEnabled(True)

    def show_details(self):
        notes = [f"{item.source.name} — {item.detail}" for item in self.results if item.detail]
        text = "\n".join(notes[:12])
        if len(notes) > 12:
            text += f"\nОстальные замечания ({len(notes) - 12}) указаны в журнале."
        message(self.window, "Результаты создания PDF", text, error=any(item.status == "error" for item in self.results))

    def open_folder(self):
        if self.folder and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder))):
            message(self.window, "Папка для PDF", "Не удалось открыть папку с результатами.", error=True)
