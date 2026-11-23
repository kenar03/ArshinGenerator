from dataclasses import dataclass
from datetime import date, datetime
from http.client import HTTPException
import json
import logging
import re
from time import monotonic, sleep
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://fgis.gost.ru/fundmetrology/eapi/"
ENDPOINT = API + "vri"
PUBLIC_CARD = "https://fgis.gost.ru/fundmetrology/cm/results/"
TYPE_NUMBER = re.compile(r"[0-9]{2,9}-[0-9]{2,4}")
PAGE_SIZE = 50
MAX_RECORDS = 250


class PublicDataError(ValueError):
    """The API was unreachable or the response cannot support an exact decision."""


def public_date(value) -> str:
    if isinstance(value, str):
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                pass
    return ""


class PublicClient:
    def __init__(self, opener=None, budget=18):
        self.opener = opener or urlopen
        self.deadline = monotonic() + budget

    def get(self, resource, filters=None):
        url = API + resource
        if filters:
            url += "?" + urlencode(filters)
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "ArshinGenerator/1.1"})
        endpoint = resource.split("/")[0]
        for attempt in range(2):
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise PublicDataError("Истекло время проверки публичного реестра.")
            try:
                with self.opener(request, timeout=min(6, remaining)) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("Ответ превышает допустимый размер")
                result = json.loads(raw)["result"]
                if not isinstance(result, dict):
                    raise ValueError("Нет объекта result")
                return result
            except HTTPError as exc:
                logging.warning("ФИФ ОЕИ %s: HTTP %s", endpoint, exc.code)
                if attempt or exc.code not in {429, 502, 503, 504}:
                    raise PublicDataError(f"Государственный реестр вернул HTTP {exc.code}.") from exc
                retry_after = str(exc.headers.get("Retry-After", "")) if exc.headers else ""
                if retry_after.isdecimal() and int(retry_after) > min(4, self.deadline - monotonic()):
                    raise PublicDataError("Государственный реестр ограничил частоту запросов. Повторите проверку позже.") from exc
                sleep(max(0.6, int(retry_after)) if retry_after.isdecimal() else 0.6)
            except (URLError, OSError, HTTPException) as exc:
                logging.warning("ФИФ ОЕИ %s: %s", endpoint, type(exc).__name__)
                if attempt:
                    raise PublicDataError("Нет ответа от государственного реестра.") from exc
                sleep(0.6)
            except (ValueError, TypeError, KeyError) as exc:
                raise PublicDataError("Государственный реестр вернул неполные данные.") from exc

    def items(self, resource, **filters):
        start = 0
        expected = None
        records = []
        while True:
            page = self.get(resource, {**filters, "rows": PAGE_SIZE, "start": start})
            items, count = page.get("items"), page.get("count")
            if (not isinstance(items, list) or type(count) is not int or count < 0
                    or page.get("start") != start or not all(isinstance(item, dict) for item in items)
                    or (expected is not None and count != expected)):
                raise PublicDataError("Государственный реестр вернул неполную выдачу.")
            if count > MAX_RECORDS:
                raise PublicDataError("Государственный реестр вернул слишком много вариантов. Уточните реквизиты.")
            expected = count
            records.extend(items)
            if len(records) == count:
                return records
            if not items or len(records) > count:
                raise PublicDataError("Государственный реестр вернул неполную выдачу.")
            start += len(items)

    def detail(self, resource, record_id):
        if not record_id:
            raise PublicDataError("В государственном реестре отсутствует идентификатор карточки.")
        return self.get(resource + "/" + quote(str(record_id), safe=""))


def exact_type(client: PublicClient, number: str) -> dict | None:
    matches = [item for item in client.items("mit", number=number) if item.get("number") == number]
    return matches[0] if len(matches) == 1 and matches[0].get("mit_uuid") else None


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
            public_date(item.get("verification_date")), public_date(item.get("valid_date")),
            str(item.get("result_docnum") or ""), status
        ))
    return records


def find_public_verifications(type_number: str, serial: str) -> list[PublishedVerification]:
    if not TYPE_NUMBER.fullmatch(type_number) or not 1 <= len(serial) <= 128:
        raise ValueError("Укажите регистрационный номер типа и точный заводской номер.")
    try:
        items = PublicClient().items("vri", mit_number=type_number, mi_number=serial,
                                    verification_date_end=date.today().isoformat())
        return exact_records({"result": {"items": items}}, type_number, serial)
    except PublicDataError as exc:
        raise ValueError("Публичный поиск сейчас недоступен. Проверьте подключение или используйте "
                         "подтверждённое соответствие из локального справочника.") from exc
