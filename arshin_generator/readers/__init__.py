from hashlib import sha256
from io import BytesIO
import logging
from pathlib import Path

from openpyxl import load_workbook

from ..domain import Protocol
from . import rtd

EXTENSIONS = {".xlsx", ".xlsm", ".xltm", ".xltx"}
READERS = (rtd,)


def read_protocol(path: Path) -> Protocol:
    path = path.resolve()
    if path.suffix.casefold() not in EXTENSIONS:
        return Protocol(path, "", error="Поддерживаются книги Excel XLSM, XLSX, XLTM и XLTX.", unsupported=True)
    protocol = Protocol(path, "")
    try:
        with path.open("rb") as stream:
            snapshot = stream.read(80_000_001)
        if len(snapshot) > 80_000_000:
            protocol.error = "Книга превышает размер 80 МБ."
            return protocol
        protocol.digest = sha256(snapshot).hexdigest()
        workbook = load_workbook(BytesIO(snapshot), read_only=True, data_only=True, keep_links=False)
        try:
            for reader in READERS:
                if reader.matches(workbook):
                    reader.read(workbook, protocol)
                    return protocol
        finally:
            workbook.close()
        protocol.unsupported = True
        protocol.error = "Структура книги не соответствует ни одному поддерживаемому формату протокола."
    except Exception:
        logging.exception("Ошибка чтения %s", path)
        protocol.error = "Книга не прочитана. Проверьте формат, доступ к файлу и сохранение в Excel."
    return protocol
