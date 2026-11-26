from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
import sqlite3
from time import monotonic, time

from .domain import RegistryEntry
from .public_registry import REGISTRY_TTL, PublicClient, PublicDataError, SharedResponses, exact_type
from .registry import EquipmentCandidate, EquipmentLookup, lookup_equipment


@dataclass(frozen=True)
class TypeLookup:
    title: str = ""
    issue: str = ""
    retryable: bool = False


class RegistrySession:
    """Keep verified results for 24 hours, including across launches with the same database."""

    def __init__(self, *, ttl=REGISTRY_TTL, clock=monotonic, wall_clock=time, opener=None, storage=None):
        self.ttl = ttl
        self.clock = clock
        self.wall_clock = wall_clock
        self.opener = opener
        self.storage = storage
        self.mit = SharedResponses(ttl, clock)
        self.types = {}
        self.equipment = {}
        if self.storage is not None:
            self.restore()

    def restore(self):
        now = self.wall_clock()
        expires = self.clock() + self.ttl
        try:
            rows = self.storage.registry_cache(now - self.ttl, now)
        except sqlite3.Error:
            logging.exception("Не удалось прочитать кеш ФИФ ОЕИ")
            return
        for row in rows:
            try:
                key = json.loads(row["lookup_key"])
                data = json.loads(row["data"])
                expiry = expires - (now - row["checked_at"])
                if row["kind"] == "type" and isinstance(key, str):
                    result = TypeLookup(**data)
                    if result.title:
                        self.types[key] = expiry, result
                elif row["kind"] == "equipment" and isinstance(key, list):
                    candidates = tuple(EquipmentCandidate(RegistryEntry(**item["entry"]), item["status"], item["eligible"])
                                       for item in data["candidates"])
                    result = EquipmentLookup(data["state"], data["message"], candidates)
                    if result.state in {"found", "choice"} and any(c.eligible for c in result.candidates):
                        self.equipment[tuple(key)] = expiry, result
            except (KeyError, TypeError, ValueError):
                logging.warning("Пропущена повреждённая запись кеша ФИФ ОЕИ")

    def _active(self, entries, keys):
        now = self.clock()
        active = {}
        for key in keys:
            if key in entries:
                expiry, value = entries[key]
                if expiry is None or expiry > now:
                    active[key] = value
        return active

    def snapshot(self, requests, numbers):
        return self._active(self.equipment, requests), self._active(self.types, numbers)

    def pending(self, requests, numbers, retry=None):
        equipment, types = self.snapshot(requests, numbers)
        retry_equipment, retry_type = retry or ((), ())
        return (
            {key: request for key, request in requests.items()
             if key not in equipment or (key in retry_equipment and self.equipment[key][0] is None)},
            {number for number in numbers
             if number not in types or (number in retry_type and self.types[number][0] is None)},
        )

    def next_expiry(self, requests, numbers):
        now = self.clock()
        expiries = [expiry for entries, keys in ((self.equipment, requests), (self.types, numbers))
                    for key in keys if key in entries
                    for expiry, _ in (entries[key],) if expiry is not None and expiry > now]
        return min(expiries, default=None)

    def remember(self, equipment, types, *, confirmed=None):
        now = self.clock()
        verified, invalidated = [], []
        for entries in (self.equipment, self.types):
            for key, (expiry, _) in list(entries.items()):
                if expiry is not None and expiry <= now:
                    del entries[key]
        if confirmed is None:
            confirmed = {key for key, result in equipment.items() if result.state == "found"
                         and sum(c.eligible for c in result.candidates) == 1}
        verified_types = {number for number, result in types.items() if result.title}
        for kind, answers, entries, successful in (("equipment", equipment, self.equipment, confirmed),
                                                    ("type", types, self.types, verified_types)):
            for key, result in answers.items():
                if key in successful:
                    entries[key] = now + self.ttl, result
                    verified.append((kind, key, result))
                else:
                    # Preserve a previous successful on-disk result through a temporary outage.
                    # Report the failure for the current session so that a new check can be requested.
                    entries[key] = None, result
                    if not result.retryable:
                        invalidated.append((kind, key))
        if self.storage is not None:
            try:
                self.storage.save_registry_cache(self.wall_clock(), verified, invalidated)
            except sqlite3.Error:
                logging.exception("Не удалось сохранить кеш ФИФ ОЕИ")
        return len(verified), len(equipment) + len(types) - len(verified)

    def check(self, requests, numbers, cancelled=lambda: False, *, force=False):
        if force:
            self.mit = SharedResponses(self.ttl, self.clock)
        else:
            # A requested type must receive a fresh response before its own TTL is renewed.
            self.mit.discard(numbers)
        wave = SharedResponses(self.ttl, self.clock, cache_errors=True)

        def client(budget):
            return PublicClient(self.opener, budget, type_cache=self.mit, response_cache=wave)

        def find_type(number):
            if not cancelled():
                try:
                    record = exact_type(client(20), number)
                    if record is None:
                        return TypeLookup(issue=f"В ФИФ ОЕИ не найден утверждённый тип СИ с номером «{number}».")
                    title = record.get("title")
                    if isinstance(title, str) and title.strip():
                        return TypeLookup(title=title.strip())
                except PublicDataError as exc:
                    if exc.kind == "connection":
                        return TypeLookup(issue=f"Не удалось проверить тип СИ «{number}». Нет связи с ФИФ ОЕИ.",
                                          retryable=True)
                return TypeLookup(issue=f"Не удалось подтвердить тип СИ «{number}» по ответу ФИФ ОЕИ.",
                                  retryable=True)
            return TypeLookup(issue="Проверка типа СИ остановлена.", retryable=True)

        def find_equipment(request):
            if cancelled():
                return EquipmentLookup("unavailable", "Проверка средства поверки остановлена")
            try:
                item, day = request
                result = lookup_equipment(item, day, client(100))
                logging.info("Сверка оборудования: %s, Excel %s, номер=%s, дата=%s, состояние=%s, причина=%s, карточки=%s",
                             item.category, item.source, item.registration or item.type_number, day, result.state,
                             result.message or "проверено",
                             [(c.entry.public_id, c.entry.valid_until, c.status) for c in result.candidates])
                return result
            except Exception:
                logging.exception("Ошибка проверки публичной карточки")
                return EquipmentLookup("response", "Ответ реестра не удалось проверить")

        with ThreadPoolExecutor(max_workers=2) as workers:
            types = {number: workers.submit(find_type, number) for number in sorted(numbers)}
            equipment = dict(zip(requests, workers.map(find_equipment, requests.values())))
            checked_types = {number: future.result() for number, future in types.items()}
            for number, result in checked_types.items():
                logging.info("Сверка типа: номер=%s, результат=%s, повтор=%s",
                             number, result.title or result.issue, result.retryable)
            return equipment, checked_types
