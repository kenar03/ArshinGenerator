from hashlib import sha256
from io import BytesIO
import logging
from pathlib import Path
from typing import Protocol as ReaderContract

from openpyxl import Workbook, load_workbook

from ..domain import PreviewField, Protocol
from . import rtd

EXTENSIONS = {".xlsx", ".xlsm", ".xltm", ".xltx"}


class ProtocolReader(ReaderContract):
    VERSION: str
    DISPLAY_NAME: str
    PREVIEW_FIELDS: tuple[PreviewField, ...]

    def matches(self, book: Workbook) -> bool: ...

    def read(self, book: Workbook, protocol: Protocol) -> None: ...


READERS: tuple[ProtocolReader, ...] = (rtd,)


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
            matches = [reader for reader in READERS if reader.matches(workbook)]
            if len(matches) == 1:
                reader = matches[0]
                protocol.reader = reader.VERSION
                protocol.reader_name = reader.DISPLAY_NAME
                protocol.preview_fields = reader.PREVIEW_FIELDS
                reader.read(workbook, protocol)
                return protocol
        finally:
            workbook.close()
        protocol.unsupported = True
        if matches:
            names = ", ".join(reader.DISPLAY_NAME for reader in matches)
            protocol.error = f"Книга подходит под несколько форматов ({names}). Проверьте структуру протокола."
        else:
            protocol.error = "Структура книги не соответствует ни одному поддерживаемому формату протокола."
    except Exception:
        logging.exception("Ошибка чтения %s", path)
        protocol.error = "Книга не прочитана. Проверьте формат, доступ к файлу и сохранение в Excel."
    return protocol
