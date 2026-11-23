from datetime import date
import re

from .domain import Equipment, RegistryEntry

CATEGORIES = {
    "mieta": "СИ, применяемое в качестве эталона",
    "uve": "Эталон единицы величины",
    "npe": "Государственный первичный эталон",
    "mis": "СИ, применяемое при поверке",
}


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


def resolve(item: Equipment, entries: list[RegistryEntry], when: date | None) -> tuple[RegistryEntry | None, str]:
    matches = [
        entry for entry in entries
        if item.description == entry.description
        and (not item.source_id or entry.local_id == item.source_id)
        and (not item.serial or entry.serial == item.serial)
        and not registry_issues(entry)
    ]
    valid = [entry for entry in matches
             if when and iso_date(entry.valid_from) <= when <= iso_date(entry.valid_until)]
    if len(valid) == 1:
        return valid[0], ""
    if len(valid) > 1:
        return None, "Несколько действующих соответствий; ответственный сотрудник должен уточнить карточку"
    if matches:
        return None, "Нет подтверждённого срока действия на дату поверки"
    return None, "Нет точного подтверждённого соответствия ФИФ ОЕИ"
