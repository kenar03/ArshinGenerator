import logging
from multiprocessing import get_context
import os
from pathlib import Path
import sys
from time import monotonic
import traceback

from pypdf import PdfWriter

from . import readers
from .domain import PdfError
from .pdf_export import ExcelInterrupted, ExcelUnavailable, sheet_path

START_TIMEOUT = 35
BOOK_TIMEOUT = 120
CLOSE_TIMEOUT = 5
PROCESS_TERMINATE_AND_WAIT = 0x00100001


def render_workbook(app, adapter, source, output, missing):
    try:
        # Positional values give the same Open signature with either dynamic or generated COM bindings.
        book = app.Workbooks.Open(str(source), 0, True, missing, "", "", True, missing, missing,
                                  True, False, missing, False, False, 0)
    except Exception as exc:
        raise PdfError("Excel не открыл книгу. Проверьте права доступа, пароль и целостность файла.") from exc
    try:
        if not book.ReadOnly or os.path.normcase(os.path.abspath(book.FullName)) != os.path.normcase(str(source)):
            raise PdfError("Excel не открыл исходную книгу в режиме чтения. Используйте сохранённый протокол.")
        app.CalculateFull()
        sheets = adapter(book, source)
        if not sheets:
            raise PdfError("Модуль протокола не указал листы для печати.")
        parts = []
        for index, name in enumerate(sheets):
            sheet = book.Worksheets(name)
            part = output if len(sheets) == 1 else sheet_path(output, index)
            # Type=PDF, Quality=standard, respect the print area and keep each document closed.
            sheet.ExportAsFixedFormat(0, str(part), 0, True, False, missing, missing, False)
            parts.append(part)
            sheet = None
        if len(parts) > 1:
            with PdfWriter() as writer:
                for part in parts:
                    writer.append(part, import_outline=False)
                writer.write(output)
    finally:
        try:
            book.Close(False)
        except Exception as exc:
            raise ExcelInterrupted("Excel не закрыл книгу. Сеанс печати будет перезапущен.") from exc
        finally:
            book = None


def _worker(connection):
    import pythoncom
    import win32api
    import win32com.client
    import win32process

    app = owner = None
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        existing = set(win32process.EnumProcesses())
        app = win32com.client.DispatchEx("Excel.Application")
        _, pid = win32process.GetWindowThreadProcessId(app.Hwnd)
        if not pid or pid in existing:
            raise ExcelUnavailable("Excel не создал отдельный экземпляр для печати.")
        handle = win32api.OpenProcess(PROCESS_TERMINATE_AND_WAIT | 0x410, False, pid)
        if Path(win32process.GetModuleFileNameEx(handle, 0)).name.casefold() != "excel.exe":
            handle.Close()
            raise ExcelUnavailable("Не удалось подтвердить отдельный экземпляр Excel.")
        owner = handle
        connection.send(("owner", pid))
        app.Visible = False
        app.DisplayAlerts = False
        app.EnableEvents = False
        app.ScreenUpdating = False
        app.AskToUpdateLinks = False
        app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        connection.send(("ready", ""))
        while (request := connection.recv()) is not None:
            version, source, output = request
            module = next((module for module in readers.READERS if module.VERSION == version), None)
            adapter = getattr(module, "prepare_pdf", None)
            if not callable(adapter):
                connection.send(("error", "Модуль протокола не поддерживает PDF.", ""))
                continue
            try:
                render_workbook(app, adapter, source, output, pythoncom.Missing)
            except ExcelInterrupted as exc:
                connection.send(("restart", str(exc), traceback.format_exc()))
                break
            except PdfError as exc:
                connection.send(("error", str(exc), traceback.format_exc()))
            except Exception as exc:
                lost = getattr(exc, "hresult", None) in {-2147417848, -2147220995, -2147023174, -2147023170}
                reason = ("Потеряна связь с Excel. Сеанс печати будет перезапущен." if lost else
                          "Excel не подготовил или не сохранил печатную форму. Проверьте область печати и настройки листа.")
                connection.send(("restart" if lost else "error", reason, traceback.format_exc()))
                if lost:
                    break
            else:
                connection.send(("done", "", ""))
    except (BrokenPipeError, EOFError):
        pass
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ExcelUnavailable) else "Microsoft Excel не установлен или недоступен через COM."
        connection.send(("unavailable", reason, traceback.format_exc()))
    finally:
        try:
            if owner is not None:
                app.Quit()
        finally:
            app = None
            if owner is not None:
                owner.Close()
            pythoncom.CoUninitialize()
            connection.close()


class ExcelSession:
    """Supervise blocking COM calls in an isolated child and retain a handle to its verified Excel process."""

    def __init__(self):
        if sys.platform != "win32":
            raise ExcelUnavailable("Для создания PDF нужен Microsoft Excel для Windows.")
        try:
            import win32api
        except ImportError as exc:
            raise ExcelUnavailable("Компоненты автоматизации Excel недоступны. Переустановите «СИ-Экспорт».") from exc

        self.owner = None
        context = get_context("spawn")
        self.connection, endpoint = context.Pipe()
        self.process = context.Process(target=_worker, args=(endpoint,), daemon=True)
        try:
            self.process.start()
            endpoint.close()
            deadline = monotonic() + START_TIMEOUT
            reply = self._receive(deadline - monotonic())
            if reply[0] == "owner":
                self.owner = win32api.OpenProcess(PROCESS_TERMINATE_AND_WAIT, False, reply[1])
                reply = self._receive(deadline - monotonic())
            if reply[0] != "ready" or self.owner is None:
                if len(reply) > 2:
                    logging.warning("Запуск COM Excel: %s", reply[2])
                raise ExcelUnavailable(reply[1] or "Не удалось запустить отдельный экземпляр Excel.")
        except ExcelInterrupted as exc:
            self.close(force=True)
            raise ExcelUnavailable("Excel не ответил при запуске. Проверьте его работу и повторите операцию.") from exc
        except Exception as exc:
            self.close()
            if isinstance(exc, ExcelUnavailable):
                raise
            raise ExcelUnavailable("Не удалось запустить отдельный экземпляр Excel.") from exc
        finally:
            endpoint.close()

    def _receive(self, timeout):
        try:
            if self.connection.poll(max(0, timeout)):
                return self.connection.recv()
        except (OSError, EOFError) as exc:
            raise ExcelInterrupted("Связь с Excel прервалась. Сеанс печати будет перезапущен.") from exc
        raise ExcelInterrupted("Excel не ответил вовремя. Сеанс печати будет перезапущен.")

    def render(self, version, source, output):
        try:
            self.connection.send((version, source, output))
        except OSError as exc:
            raise ExcelInterrupted("Связь с Excel прервалась. Сеанс печати будет перезапущен.") from exc
        try:
            state, message, details = self._receive(BOOK_TIMEOUT)
        except ExcelInterrupted:
            self.close(force=True)
            raise
        if details:
            logging.warning("Отчёт Excel %s: %s", source.name, details)
        if state != "done":
            raise (ExcelInterrupted if state == "restart" else PdfError)(message)

    def close(self, *, force=False):
        if self.process is None:
            return
        import win32api
        import win32event
        from pywintypes import error as WindowsError

        process, self.process = self.process, None
        try:
            if process.pid is not None and not force:
                if process.is_alive():
                    try:
                        self.connection.send(None)
                    except (OSError, EOFError):
                        pass
                process.join(CLOSE_TIMEOUT)
            if self.owner is not None:
                wait = 0 if force else 1000
                if win32event.WaitForSingleObject(self.owner, wait) == win32event.WAIT_TIMEOUT:
                    logging.warning("Завершаем принадлежащий генератору экземпляр Excel по открытому дескриптору")
                    try:
                        win32api.TerminateProcess(self.owner, 1)
                        win32event.WaitForSingleObject(self.owner, 2000)
                    except WindowsError:
                        if win32event.WaitForSingleObject(self.owner, 0) == win32event.WAIT_TIMEOUT:
                            logging.exception("Не удалось завершить экземпляр Excel")
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(CLOSE_TIMEOUT)
                if not process.is_alive():
                    process.close()
            if self.owner is not None:
                self.owner.Close()
                self.owner = None
            self.connection.close()
