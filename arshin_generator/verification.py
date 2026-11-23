from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from time import monotonic

from .public_registry import REGISTRY_TTL, PublicClient, PublicDataError, SharedResponses, official_type_title
from .registry import EquipmentLookup, lookup_equipment


@dataclass(frozen=True)
class TypeLookup:
    title: str = ""
    issue: str = ""
    retryable: bool = False


class RegistrySession:
    """Keep verified results in memory. An elapsed result is never usable for export."""

    def __init__(self, *, ttl=REGISTRY_TTL, clock=monotonic, opener=None):
        self.ttl = ttl
        self.clock = clock
        self.opener = opener
        self.mit = SharedResponses(ttl, clock)
        self.types = {}
        self.equipment = {}

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
             if number not in types or (number in retry_type and types[number].retryable)},
        )

    def next_expiry(self, requests, numbers):
        expiries = [entries[key][0] for entries, keys in ((self.equipment, requests), (self.types, numbers))
                    for key in keys if key in entries and entries[key][0] is not None]
        return min(expiries, default=None)

    def remember(self, equipment, types):
        now = self.clock()
        for entries in (self.equipment, self.types):
            for key, (expiry, _) in list(entries.items()):
                if expiry is not None and expiry <= now:
                    del entries[key]
        for key, result in equipment.items():
            expiry = None if result.state in {"unavailable", "response"} else now + self.ttl
            self.equipment[key] = expiry, result
        for number, result in types.items():
            self.types[number] = (None if result.retryable else now + self.ttl), result

    def check(self, requests, numbers, cancelled=lambda: False):
        wave = SharedResponses(self.ttl, self.clock, cache_errors=True)

        def client(budget):
            return PublicClient(self.opener, budget, type_cache=self.mit, response_cache=wave)

        def find_type(number):
            if not cancelled():
                try:
                    title = official_type_title(number, client(20))
                    return TypeLookup(title=title) if title else TypeLookup(
                        issue=f"В реестре не найден точный тип СИ {number}. Проверьте номер в Excel.")
                except PublicDataError:
                    return TypeLookup(issue=f"Не удалось проверить тип СИ {number} в ФИФ ОЕИ.", retryable=True)
            return TypeLookup(issue="Проверка типа СИ остановлена.", retryable=True)

        def find_equipment(request):
            if cancelled():
                return EquipmentLookup("unavailable", "Проверка средства поверки остановлена")
            try:
                item, day = request
                return lookup_equipment(item, day, client(100))
            except Exception:
                logging.exception("Ошибка проверки публичной карточки")
                return EquipmentLookup("response", "Ответ реестра не удалось проверить")

        with ThreadPoolExecutor(max_workers=2) as workers:
            types = {number: workers.submit(find_type, number) for number in sorted(numbers)}
            equipment = dict(zip(requests, workers.map(find_equipment, requests.values())))
            return equipment, {number: future.result() for number, future in types.items()}
