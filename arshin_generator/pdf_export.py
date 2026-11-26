from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from tempfile import TemporaryFile, mkstemp
from zipfile import ZipFile

from lxml import etree
from openpyxl import load_workbook
from pypdf import PdfReader

from .domain import PdfError
from .readers import EXTENSIONS, matching_readers


class ExcelUnavailable(PdfError):
    pass


class ExcelInterrupted(PdfError):
    pass


@dataclass(frozen=True)
class PdfJob:
    source: Path
    target: Path
    existing: tuple | None
    duplicate: bool


@dataclass(frozen=True)
class PdfResult:
    source: Path
    status: str
    detail: str = ""


def _file_id(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def check_folder(folder):
    if not folder.is_absolute() or not folder.is_dir():
        raise OSError("Папка для PDF недоступна.")
    with TemporaryFile(dir=folder):
        pass


def plan_pdf(paths, folder):
    seen_sources = set()
    names = Counter()
    jobs = []
    for path in paths:
        source = path.absolute()
        key = os.path.normcase(str(source))
        if key in seen_sources:
            continue
        seen_sources.add(key)
        target = folder / (source.stem + ".pdf")
        names[target.name.casefold()] += 1
        try:
            existing = _file_id(target)
        except FileNotFoundError:
            existing = None
        jobs.append(PdfJob(source, target, existing, names[target.name.casefold()] > 1))
    return jobs


def print_reader(source):
    if source.suffix.casefold() not in EXTENSIONS:
        raise PdfError("Формат книги не поддерживается. Используйте XLSX, XLSM, XLTX или XLTM.")
    try:
        with source.open("rb") as stream:
            if stream.read(8) == bytes.fromhex("d0cf11e0a1b11ae1"):
                raise PdfError("Книга защищена паролем или имеет несовместимый формат. Проверьте её в Excel.")
            stream.seek(0)
            with ZipFile(stream) as archive:
                for member in archive.namelist():
                    lower = member.casefold()
                    if lower.startswith("xl/macrosheets/"):
                        raise PdfError("Книга содержит листы макросов Excel 4.0. PDF для такой книги недоступен.")
                    if lower == "xl/connections.xml" or lower.startswith("xl/querytables/"):
                        parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
                        root = etree.fromstring(archive.read(member), parser)
                        if any(node.get("refreshOnLoad", "").casefold() in {"1", "true"} for node in root.iter()):
                            raise PdfError("В книге настроено обновление внешних данных при открытии. Проверьте параметры подключений в Excel.")
            stream.seek(0)
            book = load_workbook(stream, read_only=True, data_only=True, keep_links=False)
            try:
                readers = matching_readers(book)
            finally:
                book.close()
    except PdfError:
        raise
    except (PermissionError, FileNotFoundError) as exc:
        raise PdfError("Книга недоступна для чтения. Проверьте путь, права доступа и блокировку Excel.") from exc
    except Exception as exc:
        raise PdfError("Не удалось прочитать книгу. Проверьте её целостность в Excel.") from exc
    if len(readers) != 1:
        raise PdfError("Не удалось определить тип протокола. Проверьте листы книги и соответствие шаблону.")
    reader = readers[0]
    if not callable(getattr(reader, "prepare_pdf", None)):
        raise PdfError(f"Модуль «{reader.DISPLAY_NAME}» не поддерживает PDF.")
    return reader


def verify_pdf(path):
    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Missing PDF header")
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted or not reader.pages:
                raise ValueError("Missing readable pages")
            for page in reader.pages:
                page.get_contents()
    except Exception as exc:
        raise PdfError("Excel не создал читаемый PDF со страницами.") from exc


def _publish(temporary, job):
    if job.existing is not None:
        if not job.target.is_file() or _file_id(job.target) != job.existing:
            raise PdfError("PDF в папке назначения изменился во время обработки. Повторите операцию.")
        os.replace(temporary, job.target)
    elif os.name == "nt":
        temporary.rename(job.target)  # Windows rename fails if the destination now exists.
    else:
        os.link(temporary, job.target)


def sheet_path(output, index):
    return output.with_name(f"{output.stem}-sheet-{index}.pdf")


@contextmanager
def _staged_pdf(folder):
    descriptor, name = mkstemp(prefix=".si-export-pdf-", suffix=".pdf", dir=folder)
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
    finally:
        # A terminated Excel session can leave intermediate PDFs when printing multiple sheets.
        for path in (temporary, *folder.glob(f"{temporary.stem}-sheet-*.pdf")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.warning("Не удалось удалить временный PDF %s", path, exc_info=True)


def create_pdfs(jobs, *, replace_existing=False, progress=lambda *_: None, session_factory=None):
    if session_factory is None:
        from .excel_pdf import ExcelSession
        session_factory = ExcelSession
    results = []
    session = None
    unavailable = None
    try:
        for index, job in enumerate(jobs, 1):
            progress(job.source, index, len(jobs))
            if job.duplicate or (job.existing is not None and not replace_existing):
                reason = "Совпадающее имя в партии." if job.duplicate else "PDF с таким именем уже существует."
                logging.info("PDF %s пропущен: %s", job.source, reason)
                results.append(PdfResult(job.source, "skipped", reason))
                continue
            try:
                observed = _file_id(job.source)
                reader = print_reader(job.source)
                with _staged_pdf(job.target.parent) as temporary:
                    if unavailable:
                        raise ExcelUnavailable(unavailable)
                    if session is None:
                        session = session_factory()
                    session.render(reader.VERSION, job.source, temporary)
                    verify_pdf(temporary)
                    if _file_id(job.source) != observed:
                        raise PdfError("Исходную книгу изменили во время обработки. Сохраните её и повторите экспорт.")
                    _publish(temporary, job)
                results.append(PdfResult(job.source, "created"))
            except ExcelInterrupted as exc:
                logging.warning("PDF %s: прерван сеанс Excel", job.source, exc_info=True)
                results.append(PdfResult(job.source, "error", str(exc)))
                if session is not None:
                    session.close()
                    session = None
            except PdfError as exc:
                if isinstance(exc, ExcelUnavailable):
                    unavailable = str(exc)
                logging.warning("PDF %s: %s", job.source, exc, exc_info=True)
                results.append(PdfResult(job.source, "error", str(exc)))
            except FileNotFoundError:
                logging.exception("Файл или папка недоступны при создании PDF %s", job.source)
                results.append(PdfResult(job.source, "error", "Книга или папка недоступны. Проверьте путь и права доступа."))
            except OSError:
                logging.exception("Ошибка доступа к файлу или папке PDF %s", job.source)
                results.append(PdfResult(job.source, "error", "Ошибка записи PDF. Проверьте папку и доступ к файлу."))
    finally:
        if session is not None:
            session.close()
    return results
