from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
import shutil
from uuid import uuid4

from lxml import etree

from .domain import Result
from .normalization import CANONICAL_NAME, serial
from .validation import RULES

NS = "urn://fgis-arshin.gost.ru/module-verifications/import/2020-06-19"
SCHEMA_NAME = "import.2022-12-21.xsd"
SCHEMA_FILE = Path(__file__).parent / "schemas" / SCHEMA_NAME
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


def result_element(result: Result) -> etree._Element:
    v = result.value
    result_tag = etree.Element(f"{{{NS}}}result", nsmap={"gost": NS})
    mi = _add(_add(result_tag, "miInfo"), "singleMI")
    _add(mi, "mitypeNumber", v("type_number"))
    _add(mi, "manufactureNum", result.instrument.serial)
    if v("manufacture_year"):
        _add(mi, "manufactureYear", v("manufacture_year"))
    _add(mi, "modification", v("modification"))
    _add(result_tag, "signCipher", v("sign_cipher"))
    if v("owner"):
        _add(result_tag, "miOwner", v("owner"))
    _add(result_tag, "vrfDate", v("verification_date"))
    if v("outcome") == "pass" and v("valid_date"):
        _add(result_tag, "validDate", v("valid_date"))
    _add(result_tag, "type", v("verification_type"))
    _add(result_tag, "calibration", v("calibration"))
    if v("outcome") == "pass":
        outcome = _add(result_tag, "applicable")
        for tag, key in (("stickerNum", "sticker"), ("signPass", "sign_pass"), ("signMi", "sign_mi")):
            _add(outcome, tag, v(key))
    elif v("outcome") == "fail":
        _add(_add(result_tag, "inapplicable"), "reasons", v("reasons"))
    _add(result_tag, "docTitle", v("method"))
    _add(result_tag, "metrologist", v("metrologist"))
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
    for tag, key in (("temperature", "temperature"), ("pressure", "pressure"), ("hymidity", "humidity")):
        _add(conditions, tag, v(key))
    if v("other"):
        _add(conditions, "other", v("other"))
    if v("structure"):
        _add(result_tag, "structure", v("structure"))
    if v("brief"):
        _add(_add(result_tag, "brief_procedure"), "characteristics", v("brief"))
    if v("additional"):
        _add(result_tag, "additional_info", v("additional"))
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
            message = "Проверьте порядковый номер наклейки из 11 цифр у пригодного СИ."
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
        self.schema = etree.XMLSchema(_parse(data))

    def inspect(self, data: bytes):
        root = _parse(data)
        if self.schema.validate(root):
            return root, [], []
        errors = self.schema.error_log
        return root, friendly(errors), [str(error) for error in errors]


def format_packets(results: list[Result], schema: Schema, limit: int = TARGET_BYTES):
    if not results or any(r.issues for r in results):
        raise ValueError("В пакет можно включить только проверенные рабочие результаты.")
    if len({(r.identity[0], r.identity[1].upper(), r.identity[2]) for r in results}) != len(results):
        raise ValueError("В пакете обнаружены одинаковые результаты.")
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


def export(results, schema, destination: Path, storage, excluded, confirmer):
    packets = format_packets(results, schema)
    token = uuid4().hex[:12]
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    final = destination / f"Аршин_{stamp}_{token}"
    stage = destination / f".arshin-{token}.tmp"
    stage.mkdir()
    files = []
    moved = False
    try:
        audit_items = []
        for index, (payload, expected) in enumerate(packets, 1):
            name = f"Аршин_{index:03d}.xml"
            files.append(name)
            (stage / name).write_bytes(payload)
            verify_disk_content((stage / name).read_bytes(), expected, schema)
            for result, record in expected:
                audit_items.append((result.identity, sha256(repr(record).encode()).hexdigest(), {
                    "protocol": result.protocol.number, "reader": result.protocol.reader,
                    "file": name, "source": str(result.protocol.path), "digest": result.protocol.digest,
                    "serial": asdict(result.instrument),
                    "fields": {k: asdict(value) for k, value in result.fields.items()},
                    "equipment": [asdict(item) for item in result.equipment],
                }))
        stage.rename(final)
        moved = True
        for name, (_, expected) in zip(files, packets):
            verify_disk_content((final / name).read_bytes(), expected, schema)
        storage.record_export(token, final, files, schema.hash, RULES, excluded, confirmer, audit_items)
        return [final / name for name in files]
    except Exception:
        shutil.rmtree(final if moved else stage)
        raise


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
