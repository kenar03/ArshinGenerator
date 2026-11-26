import argparse
import ctypes
from importlib.resources import files
import json
import logging
from logging.handlers import RotatingFileHandler
from multiprocessing import freeze_support
from pathlib import Path
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QLockFile, Qt, QTimer, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from arshin_generator import APP_NAME, VERSION
from arshin_generator.arshin import Schema
from arshin_generator.public_registry import check_connection
from arshin_generator.storage import Storage, data_directory
from arshin_generator.ui.common import message
from arshin_generator.ui.dialogs import HistoryDialog, XmlDialog
from arshin_generator.ui.main import MainWindow
from arshin_generator.ui.style import apply_theme


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--smoke-check", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-pdf", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--check-connection", type=Path, help=argparse.SUPPRESS)
    service = parser.add_mutually_exclusive_group()
    service.add_argument("--history", action="store_true", help="Открыть историю выгрузок и резервное копирование")
    service.add_argument("--view-xml", type=Path, help="Открыть сохранённый XML")
    arguments = parser.parse_args(argv)
    if arguments.smoke_pdf:
        if not arguments.smoke_check or not arguments.paths:
            parser.error("--smoke-pdf requires --smoke-check and input files")
        from arshin_generator.pdf_export import create_pdfs, plan_pdf

        logging.basicConfig(level=logging.INFO, filename=arguments.smoke_check.with_suffix(".log"),
                            encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
        jobs = plan_pdf(arguments.paths, arguments.smoke_pdf.resolve())
        results = create_pdfs(jobs)
        report = {"version": VERSION, "results": [dict(file=result.source.name, status=result.status,
                                                        detail=result.detail) for result in results]}
        arguments.smoke_check.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return int(any(result.status != "created" for result in results))
    if arguments.check_connection:
        log = arguments.check_connection.with_suffix(".log")
        logging.basicConfig(level=logging.INFO, filename=log, encoding="utf-8",
                            format="%(asctime)s %(levelname)s %(message)s")
        report = {"version": VERSION, "registries": check_connection()}
        arguments.check_connection.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return int(any(status != "Доступен" for status in report["registries"].values()))
    if sys.platform == "win32":
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id("ArshinGenerator.Desktop")
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    apply_theme(app)
    app.setWindowIcon(QIcon(str(files("arshin_generator").joinpath("assets", "app.ico"))))
    locale = QLocale("ru_RU")
    QLocale.setDefault(locale)
    translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(app)
    if translator.load(locale, "qtbase", "_", translations):
        app.installTranslator(translator)
    try:
        data = data_directory()
        data.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(data / "diagnostics.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(message)s")
        lock = QLockFile(str(data / "session.lock"))
        if not lock.tryLock(0):
            raise ValueError("Эта папка данных занята другой копией приложения.")
        storage = Storage(data / "arshin.sqlite3")
        schema = Schema()
    except Exception as exc:
        logging.exception("Ошибка запуска")
        if arguments.smoke_check:
            arguments.smoke_check.write_text(json.dumps({"error": str(exc)}, ensure_ascii=False), encoding="utf-8")
        else:
            message(None, "Запуск приложения", "Не удалось открыть папку данных или схему XML.\n" + str(exc), error=True)
        return 1
    if arguments.history or arguments.view_xml:
        dialog = (HistoryDialog(None, storage, schema) if arguments.history else
                  XmlDialog(None, [arguments.view_xml.resolve()], schema, {}))
        dialog.exec()
        lock.unlock()
        return 0
    window = MainWindow(storage, schema, online=not arguments.smoke_check)
    if arguments.smoke_check:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.show()
    if arguments.paths:
        QTimer.singleShot(0, lambda: window.import_paths(arguments.paths))
    if arguments.smoke_check:
        def verify():
            if window.thread is not None:
                QTimer.singleShot(100, verify)
                return
            previous = storage.setting("smoke_check_count")
            storage.set_setting("smoke_check_count", str(int(previous or "0") + 1))
            automatic, auto_folder = storage.xml_output()
            report = {"version": VERSION, "name": app.applicationName(), "data": str(data), "schema": schema.hash,
                      "icon_sizes": sorted(size.width() for size in app.windowIcon().availableSizes()),
                      "previous": previous, "input_folder": storage.setting("input_folder"),
                      "output_folder": storage.setting("output_folder"), "xml_automatic": automatic,
                      "xml_auto_folder": auto_folder,
                      "protocols": len(window.protocols),
                      "equipment": sum(len(protocol.equipment) for protocol in window.protocols),
                      "instruments": len(window.results), "ready": sum(not r.blockers for r in window.results)}
            arguments.smoke_check.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            app.quit()

        QTimer.singleShot(150, verify)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    freeze_support()
    sys.exit(main())
