from dataclasses import dataclass
import json
import logging
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ENDPOINT = "https://fgis.gost.ru/fundmetrology/eapi/vri"
PUBLIC_CARD = "https://fgis.gost.ru/fundmetrology/cm/results/"


@dataclass(frozen=True)
class PublishedVerification:
    record_id: str
    type_number: str
    serial: str
    title: str
    verification_date: str
    valid_date: str
    document: str
    outcome: str


def exact_records(response: dict, type_number: str, serial: str) -> list[PublishedVerification]:
    records = []
    seen = set()
    for item in response["result"]["items"]:
        if item.get("mit_number") != type_number or item.get("mi_number") != serial:
            continue
        record_id = str(item.get("vri_id") or "")
        if not record_id or record_id in seen:
            continue
        seen.add(record_id)
        applicable = item.get("applicability")
        status = "Пригодно" if applicable is True else "Непригодно" if applicable is False else str(applicable or "")
        records.append(PublishedVerification(
            record_id, type_number, serial, str(item.get("mit_title") or ""),
            str(item.get("verification_date") or "")[:10], str(item.get("valid_date") or "")[:10],
            str(item.get("result_docnum") or ""), status
        ))
    return records


def find_public_verifications(type_number: str, serial: str) -> list[PublishedVerification]:
    if not re.fullmatch(r"[0-9]{3,8}-[0-9]{2,4}", type_number) or not 1 <= len(serial) <= 128:
        raise ValueError("Укажите регистрационный номер типа и точный заводской номер.")
    params = urlencode({"mit_number": type_number, "mi_number": serial, "rows": 100})
    request = Request(f"{ENDPOINT}?{params}", headers={"Accept": "application/json",
                                                       "User-Agent": "ArshinGenerator/1.0"})
    try:
        with urlopen(request, timeout=8) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Ответ превышает допустимый размер.")
        return exact_records(json.loads(raw), type_number, serial)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logging.warning("Публичный поиск ФИФ ОЕИ недоступен: %s", type(exc).__name__)
        raise ValueError("Публичный поиск сейчас недоступен. Проверьте подключение или используйте "
                         "подтверждённое соответствие из локального справочника.") from exc
