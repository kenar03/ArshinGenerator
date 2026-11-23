from concurrent.futures import Future, ThreadPoolExecutor
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import logging
import re
import sys
from threading import Lock
from time import monotonic, sleep
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .http_transport import MAX_RESPONSE, windows_get

API = "https://fgis.gost.ru/fundmetrology/eapi/"
ENDPOINT = API + "vri"
PUBLIC_CARD = "https://fgis.gost.ru/fundmetrology/cm/results/"
TYPE_NUMBER = re.compile(r"[0-9]{2,9}-[0-9]{2,4}")
PAGE_SIZE = 50
MAX_RECORDS = 250
REGISTRY_TTL = 15 * 60


class PublicDataError(ValueError):
    """The API was unreachable or the response cannot support an exact decision."""

    def __init__(self, message, *, kind="response"):
        super().__init__(message)
        self.kind = kind


class SharedResponses:
    """Share in-flight requests and reuse completed responses for a bounded time."""

    def __init__(self, ttl=REGISTRY_TTL, clock=monotonic, *, cache_errors=False):
        self.ttl = ttl
        self.clock = clock
        self.cache_errors = cache_errors
        self.lock = Lock()
        self.ready = OrderedDict()
        self.pending = {}

    def get(self, key, fetch):
        with self.lock:
            now = self.clock()
            while self.ready and next(iter(self.ready.values()))[0] <= now:
                self.ready.popitem(last=False)
            if key in self.ready:
                return self.ready[key][1].result()
            existing = self.pending.get(key)
            if existing is None:
                self.pending[key] = future = Future()
        if existing is not None:
            return existing.result()
        try:
            future.set_result(fetch())
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            with self.lock:
                if self.cache_errors or future.exception() is None:
                    self.ready[key] = self.clock() + self.ttl, future
                self.pending.pop(key)
        return future.result()


def _retry_delay(header, attempt):
    if header.isdecimal() and len(header) < 9:
        return int(header)
    if header:
        try:
            until = parsedate_to_datetime(header)
            if until.tzinfo is not None:
                return max(0, (until - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 2 ** attempt


def public_date(value) -> str:
    if isinstance(value, str):
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                pass
    return ""


class PublicClient:
    def __init__(self, opener=None, budget=100, *, type_cache=None, response_cache=None):
        self.opener = opener or urlopen
        self.native = opener is None and sys.platform == "win32"
        self.deadline = monotonic() + budget
        self.type_cache = type_cache
        self.response_cache = response_cache

    def _request(self, url, timeout):
        agent = "ArshinGenerator/1.2"
        if self.native:
            status, body, retry_after = windows_get(url, agent, timeout)
            if status != 200:
                raise HTTPError(url, status, "", {"Retry-After": retry_after}, None)
            return body
        request = Request(url, headers={"Accept": "application/json", "User-Agent": agent})
        with self.opener(request, timeout=timeout) as response:
            return response.read(MAX_RESPONSE + 1)

    def get(self, resource, filters=None):
        if self.response_cache is not None and resource != "mit":
            key = resource, tuple(sorted((filters or {}).items()))
            return self.response_cache.get(key, lambda: self._get(resource, filters))
        return self._get(resource, filters)

    def _get(self, resource, filters=None):
        url = API + resource
        if filters:
            url += "?" + urlencode(filters)
        transport = "WinHTTP" if self.native else "urllib"
        for attempt in range(3):
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise PublicDataError("Истекло время проверки публичного реестра.", kind="connection")
            started = monotonic()
            try:
                raw = self._request(url, timeout=min(20, remaining))
                if len(raw) > MAX_RESPONSE:
                    raise ValueError("Ответ превышает допустимый размер")
                result = json.loads(raw)["result"]
                if not isinstance(result, dict):
                    raise ValueError("Нет объекта result")
                logging.info("ФИФ ОЕИ %s GET %s HTTP 200 %.2f с", transport, url, monotonic() - started)
                return result
            except HTTPError as exc:
                logging.warning("ФИФ ОЕИ %s GET %s HTTPError %s %.2f с", transport, url, exc.code, monotonic() - started)
                if attempt == 2 or not (exc.code == 429 or 500 <= exc.code <= 599):
                    kind = "connection" if exc.code in {401, 403, 407, 429} or 500 <= exc.code <= 599 else "response"
                    raise PublicDataError(f"Государственный реестр вернул HTTP {exc.code}.", kind=kind) from exc
                retry_after = str(exc.headers.get("Retry-After", "")) if exc.headers else ""
                delay = max(1, _retry_delay(retry_after, attempt))
                if delay > min(15, self.deadline - monotonic()):
                    raise PublicDataError("Государственный реестр просит повторить запрос позже.", kind="connection") from exc
                sleep(delay)
            except (URLError, OSError, HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                detail = re.sub(r"(https?://)[^/\s]+@", r"\1[скрыто]@", str(reason))
                code = getattr(reason, "winerror", None)
                logging.warning("ФИФ ОЕИ %s GET %s %s/%s winerror=%s %s %.2f с",
                                transport, url, type(exc).__name__, type(reason).__name__, code,
                                detail, monotonic() - started)
                if attempt == 2 or (code is not None and code not in {12002, 12007, 12029, 12030, 12031, 12152}):
                    raise PublicDataError("Нет ответа от государственного реестра.", kind="connection") from exc
                delay = 2 ** attempt
                if delay >= self.deadline - monotonic():
                    raise PublicDataError("Нет ответа от государственного реестра.", kind="connection") from exc
                sleep(delay)
            except (ValueError, TypeError, KeyError) as exc:
                logging.warning("ФИФ ОЕИ %s GET %s %s: %s", transport, url, type(exc).__name__, exc)
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
    def fetch():
        matches = [item for item in client.items("mit", number=number) if item.get("number") == number]
        return matches[0] if len(matches) == 1 and matches[0].get("mit_uuid") else None

    def shared():
        cache = getattr(client, "type_cache", None)
        return cache.get(number, fetch) if cache is not None else fetch()

    wave = getattr(client, "response_cache", None)
    return wave.get(("mit", number), shared) if wave is not None else shared()


def official_type_title(number: str, client: PublicClient | None = None) -> str:
    if not TYPE_NUMBER.fullmatch(number):
        return ""
    record = exact_type(client or PublicClient(budget=12), number)
    title = record.get("title") if record else None
    return title.strip() if isinstance(title, str) else ""


def check_connection():
    checks = [
        ("MIT", "mit", {"number": "32777-06"}, "number", "32777-06"),
        ("MIETA", "mieta", {"number": "32777.06.2Р.01504902"}, "number", "32777.06.2Р.01504902"),
        ("VRI", "vri", {"mit_number": "4542-74", "mi_number": "83046",
                        "verification_date_end": "2026-11-22"}, "mi_number", "83046"),
    ]
    def probe(check):
        name, resource, query, field, expected = check
        try:
            items = PublicClient(budget=45).items(resource, **query)
            status = "Доступен" if any(item.get(field) == expected for item in items) else "Ответ без контрольной записи"
        except PublicDataError as exc:
            status = "Нет связи" if exc.kind == "connection" else "Ответ не удалось проверить"
        return name, status

    with ThreadPoolExecutor(max_workers=len(checks)) as workers:
        return dict(workers.map(probe, checks))


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
