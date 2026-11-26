from collections import defaultdict
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from uuid import uuid4

from lxml import etree

from .domain import Result, duplicate_serials
from .normalization import CANONICAL_NAME, serial
from .validation import RULES

NS = "urn://fgis-arshin.gost.ru/module-verifications/import/2020-06-19"
SCHEMA_NAME = "import.2020-06-19.xsd"
SCHEMA_FILE = Path(__file__).parent / "schemas" / SCHEMA_NAME
SCHEMA_HASH = "cc92b2a1f8670d230a405d42f9ad08a729a6a35cab94751d5bb1208cf493e361"
PORTAL_BYTES = 5_000_000
TARGET_BYTES = 4_900_000
NS_ATTR = f' xmlns:gost="{NS}"'.encode()
HEADER = b"<?xml version='1.0' encoding='UTF-8'?>\n<gost:application" + NS_ATTR + b">\n"
FOOTER = b"</gost:application>\n"


def _parser():
    return etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                           remove_blank_text=True)


def _parse(payload: bytes) -> etree._Element:
    root = etree.fromstring(payload, parser=_parser())
    if root.getroottree().docinfo.doctype:
        raise ValueError("Файлы с объявлениями DTD не поддерживаются.")
    return root


def _add(parent, tag, value=None):
    child = etree.SubElement(parent, f"{{{NS}}}{tag}")
    if value is not None:
        child.text = str(value).lower() if isinstance(value, bool) else str(value)
    return child


def result_values(result: Result) -> dict:
    """Values used by the XML writer and the protocol preview."""
    v = result.value
    values = {
        "mitypeNumber": v("type_number"), "manufactureNum": result.instrument.serial,
        "modification": v("modification"), "signCipher": v("sign_cipher"), "vrfDate": v("verification_date"),
        "type": v("verification_type"), "calibration": v("calibration"), "docTitle": v("method"),
        "metrologist": v("metrologist"), "temperature": v("temperature"), "pressure": v("pressure"),
        "hymidity": v("humidity"),
    }
    for tag, key in (("manufactureYear", "manufacture_year"), ("miOwner", "owner"), ("other", "other"),
                     ("structure", "structure"), ("characteristics", "brief"), ("additional_info", "additional")):
        if value := v(key):
            values[tag] = value
    if v("outcome") == "pass":
        if value := v("valid_date"):
            values["validDate"] = value
        if sticker := str(v("sticker") or "").strip():
            values["stickerNum"] = sticker
        values["signPass"] = v("sign_pass")
        values["signMi"] = v("sign_mi")
    elif v("outcome") == "fail":
        values["reasons"] = v("reasons")
    return values


def result_element(result: Result) -> etree._Element:
    values = result_values(result)
    result_tag = etree.Element(f"{{{NS}}}result", nsmap={"gost": NS})
    mi = _add(_add(result_tag, "miInfo"), "singleMI")
    for tag in ("mitypeNumber", "manufactureNum", "manufactureYear", "modification"):
        if tag in values:
            _add(mi, tag, values[tag])
    for tag in ("signCipher", "miOwner", "vrfDate", "validDate", "type", "calibration"):
        if tag in values:
            _add(result_tag, tag, values[tag])
    if "signPass" in values:
        outcome = _add(result_tag, "applicable")
        for tag in ("stickerNum", "signPass", "signMi"):
            if tag in values:
                _add(outcome, tag, values[tag])
    elif "reasons" in values:
        _add(_add(result_tag, "inapplicable"), "reasons", values["reasons"])
    _add(result_tag, "docTitle", values["docTitle"])
    _add(result_tag, "metrologist", values["metrologist"])
    means = _add(result_tag, "means")
    for category in ("npe", "uve", "mieta", "mis"):
        entries = [e for e in result.equipment if e.category == category]
        if not entries:
            continue
        group = _add(means, category)
        for e in entries:
            if category == "mis":
                item = _add(group, "mi")
                _add(item, "typeNum", e.type_number)
                _add(item, e.identity_kind, e.registry_serial)
            else:
                _add(group, "number", e.identifier)
    conditions = _add(result_tag, "conditions")
    for tag in ("temperature", "pressure", "hymidity", "other"):
        if tag in values:
            _add(conditions, tag, values[tag])
    if "structure" in values:
        _add(result_tag, "structure", values["structure"])
    if "characteristics" in values:
        _add(_add(result_tag, "brief_procedure"), "characteristics", values["characteristics"])
    if "additional_info" in values:
        _add(result_tag, "additional_info", values["additional_info"])
    return result_tag


def fragment(record: etree._Element) -> bytes:
    copy = _parse(etree.tostring(record))
    etree.indent(copy, space="  ", level=1)
    return b"  " + etree.tostring(copy, encoding="UTF-8").replace(NS_ATTR, b"", 1) + b"\n"


def signature(element):
    return element.tag, element.text or "", tuple(sorted(element.attrib.items())), tuple(
        signature(child) for child in element if isinstance(child.tag, str)
    )


def friendly(entries) -> list[str]:
    messages = []
    for error in entries:
        match = re.search(r"result(?:\[(\d+)\])?", error.path or "")
        index = f", запись {match[1] or '1'}" if match else ""
        element_match = re.search(r"Element '\{[^}]+\}([^']+)'", error.message)
        element = element_match[1] if element_match else ""
        if "stickerNum" in error.message:
            message = "Проверьте формат указанного номера наклейки."
        elif element == "miOwner":
            message = "Укажите владельца СИ."
        elif element == "docTitle":
            message = "Проверьте методику поверки из листа «Отчёт»."
        elif element == "means":
            message = "Проверьте наличие средств поверки и их категорию."
        elif element:
            message = f"Проверьте поле {element}: отсутствует обязательное значение, неверен порядок или формат."
        else:
            message = "Структура или формат значения не соответствует схеме импорта."
        messages.append(f"Строка {error.line}{index}. {message}")
    return messages


class Schema:
    def __init__(self, path: Path = SCHEMA_FILE):
        data = path.read_bytes()
        self.path = path
        self.hash = sha256(data).hexdigest()
        if path == SCHEMA_FILE and self.hash != SCHEMA_HASH:
            raise ValueError("Контрольная сумма схемы 2020 года не совпадает с оригиналом.")
        self.schema = etree.XMLSchema(_parse(data))

    def inspect(self, data: bytes):
        root = _parse(data)
        if self.schema.validate(root):
            return root, [], []
        errors = self.schema.error_log
        return root, friendly(errors), [str(error) for error in errors]


def format_packets(results: list[Result], schema: Schema, limit: int = TARGET_BYTES):
    if schema.hash != SCHEMA_HASH:
        raise ValueError("Для формирования пакета применяется оригинальная XSD от 19.06.2020.")
    if not results or any(r.blockers for r in results):
        raise ValueError("В пакет можно включить только записи без блокирующих замечаний.")
    instruments = defaultdict(list)
    for result in results:
        instruments[result.protocol.path].append(result.instrument)
    if any(duplicate_serials(batch) for batch in instruments.values()):
        raise ValueError("В одном протоколе обнаружены повторяющиеся заводские номера.")
    packets = []
    fragments = []
    expected = []
    overhead = len(HEADER) + len(FOOTER)
    size = overhead
    for result in results:
        identifier = f"{result.protocol.number} / {result.instrument.serial}"
        if not CANONICAL_NAME.fullmatch(str(result.value("metrologist") or "")):
            raise ValueError(f"{identifier}: поверитель должен быть записан в формате «Фамилия И.О.».")
        if serial(result.instrument.serial) != result.instrument.serial:
            raise ValueError(f"{identifier}: проверьте латинский алфавит заводского номера.")
        try:
            record = result_element(result)
            xml = fragment(record)
        except (ValueError, etree.LxmlError) as exc:
            raise ValueError(f"{identifier}: найдены символы, недопустимые в XML.") from exc
        if len(xml) + overhead > limit:
            raise ValueError(f"Запись {result.instrument.serial} превышает размер пакета.")
        if size + len(xml) > limit:
            packets.append((HEADER + b"".join(fragments) + FOOTER, expected))
            fragments, expected, size = [], [], overhead
        fragments.append(xml)
        expected.append((result, signature(record)))
        size += len(xml)
    packets.append((HEADER + b"".join(fragments) + FOOTER, expected))
    for payload, items in packets:
        verify_disk_content(payload, items, schema)
    return packets


def verify_disk_content(payload, items, schema):
    if len(payload) > PORTAL_BYTES:
        raise ValueError("Фактический XML превышает ограничение портала 5 МБ.")
    root, messages, details = schema.inspect(payload)
    if messages:
        import logging
        logging.error("Проверка XSD: %s", "\n".join(details))
        raise ValueError("XML не прошёл проверку:\n" + "\n".join(messages[:12]))
    if [signature(child) for child in root] != [item[1] for item in items]:
        raise ValueError("Содержимое XML не совпадает с предварительным просмотром.")


def _unused_stem(stem: str, taken: set[str]):
    base = stem
    revision = 2
    while base.casefold() in taken:
        base = f"{stem}_{revision}"
        revision += 1
    return base


def split_paths(directory: Path, stem: str, count: int):
    # Reserve the entire series even if only a later part of an earlier export still exists.
    taken = {match[1] for path in directory.iterdir()
             if (match := re.fullmatch(r"(.+)_[0-9]{3,}\.xml", path.name.casefold()))}
    base = _unused_stem(stem, taken)
    return [directory / f"{base}_{index:03d}.xml" for index in range(1, count + 1)]


def automatic_paths(directory: Path, stem: str, count: int):
    if count > 1:
        return split_paths(directory, stem, count)
    taken = {path.stem.casefold() for path in directory.iterdir() if path.suffix.casefold() == ".xml"}
    return [directory / f"{_unused_stem(stem, taken)}.xml"]


def _file_identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def export(packets, schema, paths, storage, confirmer, *, overwrite=None):
    if schema.hash != SCHEMA_HASH or not paths or len(paths) != len(packets):
        raise ValueError("Пути сохранения не соответствуют проверенному пакету.")
    if len(set(paths)) != len(paths) or any(path.parent != paths[0].parent for path in paths):
        raise ValueError("Для файлов пакета нужны разные имена в одной папке.")
    if any(path.suffix.casefold() != ".xml" for path in paths):
        raise ValueError("Имя файла должно заканчиваться на .xml.")
    if overwrite is not None and len(paths) != 1:
        raise ValueError("Составной пакет сохраняется с новыми именами.")

    directory = paths[0].parent
    files = [path.name for path in paths]
    with TemporaryDirectory(prefix=".arshin-", dir=directory) as temporary:
        stage = Path(temporary)
        backup = stage / "original.bak"
        audit_items = []
        written = []
        try:
            for name, (payload, expected) in zip(files, packets):
                with (stage / name).open("wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                verify_disk_content((stage / name).read_bytes(), expected, schema)
                for position, (result, record) in enumerate(expected):
                    audit_items.append((result.identity, sha256(repr(record).encode()).hexdigest(), {
                        "protocol": result.protocol.number, "reader": result.protocol.reader,
                        "file": name, "position": position, "source": str(result.protocol.path),
                        "digest": result.protocol.digest,
                        "serial": asdict(result.instrument),
                        "fields": {key: asdict(value) for key, value in result.fields.items()},
                        "equipment": [asdict(item) for item in result.equipment],
                    }))
            for path in paths:
                pending = stage / path.name
                if overwrite is not None:
                    if _file_identity(path.stat()) != _file_identity(overwrite):
                        raise OSError("Выбранный файл изменился после подтверждения замены.")
                    shutil.copy2(path, backup)
                    if _file_identity(path.stat()) != _file_identity(overwrite):
                        raise OSError("Выбранный файл изменился после подтверждения замены.")
                    pending.replace(path)
                elif os.name == "nt":
                    pending.rename(path)  # On Windows rename fails if the destination exists.
                else:
                    os.link(pending, path)
                written.append(path)
            for path, (_, expected) in zip(paths, packets):
                verify_disk_content(path.read_bytes(), expected, schema)
            storage.record_export(uuid4().hex[:12], directory, files, schema.hash, RULES, [], confirmer, audit_items)
        except Exception:
            for path in reversed(written):
                if overwrite is not None:
                    backup.replace(path)
                else:
                    path.unlink(missing_ok=True)
            raise
    return paths


def open_xml(path: Path, schema: Schema):
    with path.open("rb") as file:
        data = file.read(20_000_001)
    if len(data) > 20_000_000:
        raise ValueError("Просмотр поддерживает файлы до 20 МБ.")
    try:
        root, issues, details = schema.inspect(data)
        if len(data) > PORTAL_BYTES:
            issues.append("Размер файла превышает 5 МБ.")
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Нарушена структура XML. Проверьте строку {exc.lineno}.") from exc
    return root, issues, details
