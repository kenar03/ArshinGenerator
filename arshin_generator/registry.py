from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256
import json
import re

from .domain import Equipment, RegistryEntry
from .public_registry import API, PublicClient, PublicDataError, TYPE_NUMBER, exact_type, public_date

CATEGORIES = {
    "mieta": "СИ, применяемые в качестве эталона",
    "uve": "Эталон единицы величины",
    "npe": "Государственный первичный эталон",
    "mis": "СИ, применяемые при поверке",
}
MIETA_NUMBER = re.compile(r"[0-9]+\.[0-9]+\.[\w]+\.[0-9]+")


@dataclass(frozen=True)
class EquipmentCandidate:
    entry: RegistryEntry
    status: str
    eligible: bool


@dataclass(frozen=True)
class EquipmentLookup:
    state: str
    message: str
    candidates: tuple[EquipmentCandidate, ...] = ()

    @property
    def retryable(self):
        return self.state in {"unavailable", "response"}


def iso_date(value) -> date | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def registry_issues(entry: RegistryEntry) -> list[str]:
    issues = []
    if not all((entry.description, entry.local_id, entry.serial, entry.name, entry.evidence, entry.confirmer)):
        issues.append("Заполните исходную строку, идентификатор, заводской номер, название, основание и сотрудника.")
    if entry.category not in CATEGORIES:
        issues.append("Выберите категорию средства поверки.")
    start, end = iso_date(entry.valid_from), iso_date(entry.valid_until)
    if not start or not end or start > end:
        issues.append("Укажите корректные даты поверки средства и окончания её действия (ГГГГ-ММ-ДД).")
    if entry.category == "mis":
        if not all((0 < len(entry.type_number) <= 32, 0 < len(entry.registry_serial) <= 128,
                    entry.identity_kind in {"manufactureNum", "inventoryNum"})):
            issues.append("Для обычного СИ укажите номер типа и точный заводской номер или обозначение в ФИФ ОЕИ.")
        if entry.type_number != entry.local_id or entry.registry_serial != entry.serial:
            issues.append("Номер типа и номер единицы обычного СИ должны точно совпадать с исходным оборудованием.")
    elif not 0 < len(entry.identifier) <= 32:
        issues.append("Для эталона укажите официальный номер конкретной записи (до 32 символов).")
    return issues


def equipment_diagnostic(item: Equipment, issue: str) -> str:
    if not issue:
        return ""
    name = item.name or item.description
    source = item.source
    if "Требуется выбор" in issue or "ранее выбранные карточки" in issue:
        issue = "Не удалось однозначно подтвердить запись ФИФ ОЕИ. Уточните реквизиты в Excel"
    else:
        issue = re.sub(r"\s*\((?:связанная )?(?:MIT|VRI|MIETA)(?:,[^)]*)?\)", "", issue)
        issue = (issue.replace("в MIT", "в ФИФ ОЕИ").replace("Карточка MIETA", "Запись ФИФ ОЕИ")
                 .replace("MIETA:", "Запись эталона:").replace("Связанная VRI:", "Связанная поверка:")
                 .replace("VRI:", "Результат поверки:"))
    return f"{name}: {issue.rstrip('.')}. Проверьте {source}."


def lookup_key(item: Equipment, day: str) -> tuple:
    return item.category, item.type_number, item.registration, item.serial, day


def _same_equipment(item: Equipment, entry: RegistryEntry) -> bool:
    return bool(item.source_id and item.serial and item.category == entry.category and
                item.source_id == entry.local_id and item.serial == entry.serial)


def _choice_matches(confirmed: RegistryEntry, online: RegistryEntry) -> bool:
    if online.category == "mieta":
        return confirmed.identifier == online.identifier
    return bool(confirmed.public_id and confirmed.public_id == online.public_id and
                confirmed.type_number == online.type_number and confirmed.registry_serial == online.registry_serial)


def resolve(item: Equipment, entries: list[RegistryEntry], when: date | None,
            online: EquipmentLookup | None = None) -> tuple[RegistryEntry | None, str]:
    matches = [
        entry for entry in entries
        if not entry.automatic and _same_equipment(item, entry) and not registry_issues(entry)
    ]
    if online and online.state in {"found", "choice", "conflict", "missing", "expired", "response"}:
        valid = [candidate.entry for candidate in online.candidates if candidate.eligible]
        if online.state == "found" and len(valid) == 1:
            return valid[0], ""
        if online.state == "choice":
            chosen = [(entry, match) for entry in valid for match in matches if _choice_matches(match, entry)]
            if len({entry.public_id for entry, _ in chosen}) == 1 and chosen:
                # The human decision selects the card; dates and identity are from the live response.
                entry, match = chosen[-1]
                return replace(entry, evidence=f"{entry.evidence} · Выбор: {match.evidence}",
                               confirmer=match.confirmer, confirmed_at=match.confirmed_at), ""
        return None, online.message
    valid = [entry for entry in matches
             if when and iso_date(entry.valid_from) <= when <= iso_date(entry.valid_until)]
    if len(valid) == 1:
        return valid[0], ""
    if len(valid) > 1:
        return None, "Несколько действующих соответствий; ответственный сотрудник должен уточнить карточку"
    if matches:
        return None, "Нет подтверждённого срока действия на дату поверки"
    if online:
        return None, online.message
    return None, "Нет точного подтверждённого соответствия ФИФ ОЕИ"


def resolve_many(item: Equipment, entries: list[RegistryEntry], when: date | None,
                 online: EquipmentLookup | None = None, *, require_public=False) -> tuple[list[RegistryEntry], str]:
    if require_public and (not online or online.state not in {"found", "choice"}):
        return [], online.message if online else "Нужна проверка ФИФ ОЕИ"
    saved = [entry for entry in entries if entry.choice_ids and _same_equipment(item, entry)
             and not registry_issues(entry) and when and iso_date(entry.valid_from) <= when <= iso_date(entry.valid_until)]
    if not saved:
        entry, reason = resolve(item, [entry for entry in entries if not entry.choice_ids], when, online)
        return ([entry] if entry else []), reason
    latest = saved[-1]
    saved = [entry for entry in saved if entry.choice_ids == latest.choice_ids and entry.confirmed_at == latest.confirmed_at]
    requested = set(latest.choice_ids)
    if {entry.public_id for entry in saved} != requested:
        return [], "Нужно уточнить ранее выбранные карточки: действует не весь набор"
    if online and online.state in {"found", "choice", "conflict", "missing", "expired", "response"}:
        matches = {}
        for candidate in online.candidates:
            if candidate.eligible:
                human = next((entry for entry in saved if _choice_matches(entry, candidate.entry)), None)
                if human:
                    actual = candidate.entry
                    matches[actual.public_id] = replace(
                        actual, evidence=f"{actual.evidence} · Выбор: {human.evidence}",
                        confirmer=human.confirmer, confirmed_at=human.confirmed_at
                    )
        if matches.keys() != requested:
            return [], online.message or "Проверьте ранее выбранные карточки"
    else:
        matches = {entry.public_id: entry for entry in saved}
    return [matches[key] for key in latest.choice_ids], ""


def _latest(rows: list[dict], when: date) -> list[dict]:
    dated = [(iso_date(public_date(row.get("verification_date"))), row) for row in rows]
    relevant = [(day, row) for day, row in dated if day is not None and day <= when]
    if not relevant:
        return []
    latest = max(day for day, _ in relevant)
    return [row for day, row in relevant if day == latest]


def _interval(row: dict, when: date) -> tuple[str, str, str]:
    start = public_date(row.get("verification_date"))
    end = public_date(row.get("valid_date"))
    if row.get("applicability") is not True:
        return start, end, "Пригодность на дату не подтверждена"
    if not start or not end:
        return start, end, "Нет подтверждённого срока действия"
    if not (iso_date(start) <= when <= iso_date(end)):
        return start, end, "Срок не покрывает дату поверки"
    return start, end, ""


def _candidate(item: Equipment, category: str, record_id: str, official_id: str, number: str,
               serial: str, model: str, scheme: str, row: dict, when: date, problem="") -> EquipmentCandidate:
    start, end, date_problem = _interval(row, when)
    proof_id = str(row.get("vri_id") or "")
    resource = "mieta" if category == "mieta" else "vri"
    proof = f"{API}{resource}/{record_id}"
    if proof_id and category == "mieta":
        proof += f" · {API}vri/{proof_id}"
    if row.get("result_docnum"):
        proof += f" · {row['result_docnum']}"
    version = sha256(json.dumps([record_id, official_id, number, serial, model, scheme, row],
                                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    entry = RegistryEntry(
        item.description, item.source_id, item.serial, item.name, category, official_id, number, serial,
        "manufactureNum", start, end, proof, "Публичный ФИФ ОЕИ", public_id=record_id, scheme=scheme, model=model,
        confirmed_at=datetime.now().astimezone().isoformat(timespec="seconds"), automatic=True, version_hash=version,
    )
    reason = problem or date_problem or ("В карточке отсутствует связанная поверка" if not proof_id else "")
    return EquipmentCandidate(entry, reason or "Подходит на дату поверки", not reason)


def _ordinary(item: Equipment, when: date, client: PublicClient) -> list[EquipmentCandidate]:
    if not exact_type(client, item.type_number):
        return []
    result = []
    listed = client.items("vri", mit_number=item.type_number, mi_number=item.serial,
                          verification_date_end=when.isoformat())
    rows = [row for row in listed if row.get("mit_number") == item.type_number and row.get("mi_number") == item.serial]
    # Keep the latest relevant result of the identified unit, including a failed one.
    latest = _latest(rows, when) or rows
    status_conflict = len({row.get("applicability") is True for row in latest}) > 1
    unknown_dates = any(not public_date(row.get("verification_date")) for row in rows)
    for row in latest:
        record_id = str(row.get("vri_id") or "")
        modification = str(row.get("mi_modification") or "").strip()
        problem = ("Поверки на одну дату дают разные результаты" if status_conflict else
                   "В выдаче не указана дата одной из поверок" if unknown_dates else "")
        result.append(_candidate(item, "mis", record_id, "", item.type_number, item.serial, modification, "",
                                 row, when, problem))
    return result


def _linked_verification(client, proof, official_id, number, serial):
    detail = client.detail("vri", proof["vri_id"])
    mi = detail.get("miInfo")
    eta = mi.get("etaMI") if isinstance(mi, dict) else None
    info = detail.get("vriInfo")
    if not isinstance(eta, dict) or not isinstance(info, dict):
        return "Связанная поверка не подтверждает категорию эталона"
    if (eta.get("regNumber"), eta.get("mitypeNumber"), eta.get("manufactureNum")) != (official_id, number, serial):
        observed_number, observed_type, observed_serial = (
            eta.get(key) or "не указан" for key in ("regNumber", "mitypeNumber", "manufactureNum")
        )
        return (f"Реквизиты эталона расходятся со связанной поверкой. MIETA: «{official_id}», «{number}», № {serial}. "
                f"VRI: «{observed_number}», «{observed_type}», № {observed_serial}")
    if (not isinstance(info.get("applicable"), dict) or info.get("inapplicable") is not None or
            public_date(info.get("vrfDate")) != public_date(proof.get("verification_date")) or
            public_date(info.get("validDate")) != public_date(proof.get("valid_date"))):
        return "Результат или срок расходятся со связанной поверкой"
    return ""


def _standards(item: Equipment, when: date, client: PublicClient) -> list[EquipmentCandidate]:
    if item.registration:
        requested = item.registration
        rows = [r for r in client.items("mieta", number=requested) if r.get("number") == requested]
    else:
        rows = [r for r in client.items("mieta", mitype_num=item.type_number, factory_num=item.serial)
                if r.get("mitype_num") == item.type_number and r.get("factory_num") == item.serial]
    if len(rows) > 30:
        raise PublicDataError("Найдено слишком много карточек эталона. Укажите полный регистрационный номер.")
    types = {}
    candidates = []
    for row in rows:
        record_id = str(row.get("rmieta_id") or "")
        detail = client.detail("mieta", record_id)
        number = str(detail.get("mitype_num") or "")
        official_id = str(detail.get("number") or "")
        serial = str(detail.get("factory_num") or "")
        scheme = " · ".join(str(detail[key]) for key in ("npenumber", "rankcode", "schematitle") if detail.get(key))
        if number not in types:
            types[number] = exact_type(client, number) if TYPE_NUMBER.fullmatch(number) else None
        modification = str(detail.get("modification") or "").strip()
        problem = ""
        if (not MIETA_NUMBER.fullmatch(official_id) or official_id != row.get("number") or
                number != row.get("mitype_num") or serial != row.get("factory_num")):
            problem = (f"Сведения о номере записи, типе или заводском номере эталона расходятся в ФИФ ОЕИ (MIETA). "
                       f"В карточке: «{official_id or 'не указан'}», «{number or 'не указан'}», «{serial or 'не указан'}»")
        elif not types[number] or (item.type_number and number != item.type_number):
            problem = "Не подтверждён регистрационный номер типа в MIT"
        elif serial != item.serial:
            problem = (f"Заводской номер расходится с протоколом. Excel: «{item.serial}». "
                       f"ФИФ ОЕИ: «{serial or 'не указан'}» (MIETA)")
        if not problem and (row.get("applicability") is not True or detail.get("applicability") is not True):
            problem = "Карточка MIETA не подтверждает пригодность"
        linked = detail.get("cresults")
        if not isinstance(linked, list) or not all(isinstance(r, dict) for r in linked):
            linked = []
        latest = _latest(linked, when)
        if len(latest) > 1:
            problem = "Несколько связанных поверок на одну дату"
        proof = latest[0] if latest else {}
        if not problem and proof.get("vri_id") and not _interval(proof, when)[2]:
            problem = _linked_verification(client, proof, official_id, number, serial)
        candidates.append(_candidate(item, "mieta", record_id, official_id, number, serial, modification, scheme,
                                     proof, when, problem or ("Нет связанной поверки на дату протокола" if not latest else "")))
    return candidates


def has_registry_identity(item: Equipment) -> bool:
    return bool(item.source_id and item.serial and item.name and (
        item.category in {"mis", "mieta"} and
        (TYPE_NUMBER.fullmatch(item.type_number) or
         (item.category == "mieta" and MIETA_NUMBER.fullmatch(item.registration)))
    ))


def lookup_equipment(item: Equipment, when: date, client: PublicClient | None = None) -> EquipmentLookup:
    if not has_registry_identity(item):
        return EquipmentLookup("manual", "Уточните реквизиты средства поверки в Excel")
    client = client or PublicClient()
    try:
        candidates = (_standards if item.category == "mieta" else _ordinary)(item, when, client)
    except PublicDataError as exc:
        if exc.kind == "connection":
            return EquipmentLookup("unavailable", "Не удалось подключиться к реестру. Повторите проверку")
        return EquipmentLookup("response", "Ответ реестра не удалось проверить. Повторите проверку")
    if not candidates:
        return EquipmentLookup("missing", "ФИФ ОЕИ не подтвердил средство поверки с указанными реквизитами")
    available = [c for c in candidates if c.eligible]
    if len(available) == 1:
        return EquipmentLookup("found", "", tuple(candidates))
    if available:
        return EquipmentLookup("choice", "Найдено несколько подходящих записей. Требуется выбор", tuple(candidates))
    if all(c.entry.valid_until and iso_date(c.entry.valid_until) < when for c in candidates):
        return EquipmentLookup("expired", "Срок поверки средства истёк", tuple(candidates))
    if len(candidates) == 1:
        candidate = candidates[0]
        hint = f" · до {candidate.entry.valid_until}" if candidate.entry.valid_until else ""
        return EquipmentLookup("conflict", f"{candidate.status}{hint}", tuple(candidates))
    return EquipmentLookup("conflict", "Ошибка: публичные карточки не подтверждают реквизиты или срок", tuple(candidates))
