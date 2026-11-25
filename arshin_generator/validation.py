from collections import Counter
import re

from . import VERSION
from .confirmations import get_confirmation, manufacture_year
from .domain import Datum, Protocol, RegistryEntry, Result
from .laboratory import LABORATORY_DEFAULTS, apply_defaults
from .normalization import metrologist
from .public_registry import TYPE_NUMBER
from .registry import equipment_diagnostic, has_registry_identity, iso_date, lookup_key, resolve_many

RULES = VERSION
INVALID_STICKER = "Указанный номер наклейки должен содержать не более 128 символов, допустимых в XML."
INVALID_YEAR = "Год изготовления должен состоять из четырёх цифр и не превышать год поверки."
XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
LABELS = {
    "type_number": "Регистрационный номер типа",
    "verification_date": "Дата поверки",
    "temperature": "Температура",
    "pressure": "Атмосферное давление",
    "humidity": "Относительная влажность",
    "modification": "Модификация",
    "owner": "Владелец СИ",
    "method": "Методика поверки",
    "metrologist": "Поверитель",
    "other": "Другие факторы",
    "structure": "Состав СИ",
    "brief": "Характеристика сокращённого объёма",
    "additional": "Прочие сведения",
    "reasons": "Причина непригодности",
}


def location(datum):
    if not datum or not datum.source:
        return ""
    sheet, _, cell = datum.source.partition("!")
    if sheet and re.fullmatch(r"[A-Z]+[0-9]+(?::[A-Z]+[0-9]+)?", cell):
        what = "ячейки" if ":" in cell else "ячейку"
        return f" Проверьте лист «{sheet}», {what} {cell}."
    return f" Проверьте Excel ({datum.source})."


def _prepare(protocol, laboratory, confirmations):
    fields = protocol.fields.copy()
    # Settings replace an older per-protocol entry only when a laboratory value was actually supplied.
    apply_defaults(fields, laboratory)
    previous = get_confirmation(confirmations, "protocol", protocol)
    legacy = {}
    if previous:
        legacy = {key: value for key, value in previous[0].items() if key not in laboratory}
        apply_defaults(fields, legacy, source="Реквизиты лаборатории, сохранённые для протокола")
    apply_defaults(fields, {key: value for key, value in LABORATORY_DEFAULTS.items()
                            if key not in laboratory and key not in legacy}, source="Значение по умолчанию")
    issues = [*protocol.issues]
    if protocol.serial_issue:
        issues.append(protocol.serial_issue)
    year = manufacture_year(protocol, confirmations)
    fields["manufacture_year"] = year
    if not valid_manufacture_year(year.value, iso_date(fields.get("verification_date", Datum(None, "")).value)):
        issues.append(INVALID_YEAR + " Проверьте год партии в просмотре протокола.")

    def field(name):
        return fields.get(name, Datum(None, ""))

    def issue(name, message):
        issues.append(message + location(protocol.fields.get(name)))

    for key in ("temperature", "pressure", "humidity"):
        if not field(key).value:
            issue(key, f"Заполните поле «{LABELS[key]}».")
    number = field("type_number").value
    valid_number = isinstance(number, str) and bool(TYPE_NUMBER.fullmatch(number))
    if not valid_number:
        raw = field("type_number").raw or number
        issue("type_number", f"Регистрационный номер типа СИ «{raw}» имеет неверный формат."
              if raw else "Укажите регистрационный номер типа СИ.")
    if not iso_date(field("verification_date").value):
        issue("verification_date", "Укажите корректную дату поверки.")
    if field("verification_type").value not in {"1", "2"}:
        issue("verification_type", "Укажите вид поверки.")
    if protocol.modification_issue:
        issues.append(protocol.modification_issue)
    elif valid_number and not field("modification").value:
        issue("modification", "Не удалось определить модификацию по данным протокола.")
    if not field("owner").value:
        issue("owner", "Укажите фактического владельца СИ.")
    if not field("method").value:
        issue("method", "Не указана методика поверки.")
    try:
        name = field("metrologist")
        fields["metrologist"] = Datum(metrologist(str(name.value or "")), name.source, name.raw)
    except ValueError:
        issue("metrologist", "Проверьте поверителя в формате «Фамилия И.О.».")
    if not re.fullmatch(r"[А-Я]{1,3}", str(field("sign_cipher").value or "")):
        issues.append("Укажите шифр знака поверки в настройках.")
    if type(field("calibration").value) is not bool:
        issues.append("Укажите в настройках, используются ли результаты калибровки.")
    if not protocol.equipment:
        issue("equipment_list", "В протоколе не указаны средства поверки.")
    for key, limit in {
        "type_number": 32, "modification": 128, "owner": 512, "method": 128,
        "metrologist": 128, "temperature": 128, "pressure": 128, "humidity": 128,
        "other": 128, "structure": 1024, "brief": 1024, "additional": 1024, "reasons": 1024,
    }.items():
        value = str(field(key).value or "")
        if len(value) > limit:
            issue(key, f"Поле «{LABELS[key]}» превышает {limit} символов по XSD.")
        if XML_FORBIDDEN.search(value):
            issue(key, f"Поле «{LABELS[key]}» содержит символы, недопустимые в XML.")
    return fields, issues


def empty_protocol_issues(protocol, laboratory, confirmations):
    if protocol.error:
        return (protocol.error,)
    _, issues = _prepare(protocol, laboratory, confirmations)
    return tuple(dict.fromkeys([*issues, "Нет сохранённых групп измерений. " + protocol.measurement_hint]))


def valid_sticker(value) -> bool:
    number = str(value or "").strip()
    return len(number) <= 128 and not XML_FORBIDDEN.search(number)


def valid_manufacture_year(value, when) -> bool:
    if value in (None, ""):
        return True
    year = str(value)
    return bool(re.fullmatch(r"[0-9]{4}", year) and int(year) > 0 and (when is None or int(year) <= when.year))


def check(protocols: list[Protocol], confirmations: dict, registry: list[RegistryEntry],
          lookups: dict | None = None, *, laboratory=None, check_registry=True,
          require_public=False, type_issues=None, pending_types=()) -> list[Result]:
    results = []
    lookups = lookups or {}
    for protocol in protocols:
        if protocol.error or not protocol.instruments:
            continue
        shared, issues = _prepare(protocol, laboratory or {}, confirmations)
        equipment = []
        equipment_resolutions = {}
        equipment_issues = []
        shared_registry_issues = []
        equipment_conflict = False
        registry_pending = shared.get("type_number", Datum(None, "")).value in pending_types
        when = iso_date(shared.get("verification_date", Datum(None, "")).value)
        for item in protocol.equipment:
            if not check_registry:
                if not has_registry_identity(item):
                    issues.append(item.input_issue or
                                  f"Уточните реквизиты средства «{item.name or item.description}». Проверьте {item.source}.")
                continue
            online = lookups.get(lookup_key(item, when.isoformat() if when else ""))
            if require_public and (online is None or online.retryable):
                registry_pending = True
                continue
            entries, error = resolve_many(item, registry, when, online, require_public=require_public)
            equipment_resolutions[item] = tuple(entries)
            if entries:
                equipment.extend(entries)
            else:
                equipment_issues.append(equipment_diagnostic(item, error or "Не удалось подтвердить сведения"))
                equipment_conflict |= online is not None and not online.retryable
        identities = [
            (e.category, e.type_number, e.registry_serial, e.identity_kind) if e.category == "mis"
            else (e.category, e.identifier) for e in equipment
        ]
        if len(set(identities)) != len(identities):
            shared_registry_issues.append("Несколько средств поверки сопоставлены с одной записью реестра.")
            equipment_conflict = True
        number = shared.get("type_number", Datum(None, "")).value
        type_issue = type_issues.get(number, "") if check_registry and type_issues else ""
        if type_issue:
            shared_registry_issues.append(type_issue + location(protocol.fields.get("type_number")))
        registry_issues = tuple(dict.fromkeys(shared_registry_issues + equipment_issues))

        failing = sum(item.outcome == "fail" for item in protocol.instruments)
        all_passed = all(item.outcome == "pass" for item in protocol.instruments)
        if failing > 1:
            issues.append("Причина непригодности нужна для каждого из нескольких СИ." + location(protocol.fields.get("reasons")))
        if all_passed and shared.get("reasons") and shared["reasons"].value:
            issues.append("При положительном результате указана причина непригодности." + location(shared["reasons"]))
        batch = []
        for instrument in protocol.instruments:
            fields = shared.copy()
            fields["manufacture_year"] = manufacture_year(protocol, confirmations, instrument)
            fields["outcome"] = Datum(instrument.outcome, instrument.source, protocol.evidence)
            if instrument.outcome != "fail" or failing != 1:
                fields.pop("reasons", None)
            if protocol.declared != 1:
                sticker = fields.pop("sticker", None)
                previous = get_confirmation(confirmations, "instrument", protocol, instrument) if sticker and sticker.value else None
                if previous and previous[0].get("sticker") == sticker.value:
                    fields["sticker"] = Datum(sticker.value, f"{sticker.source} · сохранена привязка к номеру СИ",
                                             sticker.raw)
            result = Result(protocol, instrument, fields, equipment,
                            shared_issues=tuple(dict.fromkeys(issues + list(registry_issues))),
                            equipment_issues=tuple(dict.fromkeys(equipment_issues)), registry_issues=registry_issues,
                            equipment_conflict=equipment_conflict, registry_pending=registry_pending,
                            equipment_resolutions=equipment_resolutions)
            if fields["manufacture_year"] != shared["manufacture_year"] and not valid_manufacture_year(
                result.value("manufacture_year"), when
            ):
                result.issues.append(INVALID_YEAR + " Проверьте год прибора в просмотре протокола.")
            if instrument.problem:
                result.issues.append(instrument.problem if "Проверьте" in instrument.problem else
                                     f"{instrument.problem} Проверьте {instrument.source}.")
            elif not instrument.outcome:
                result.issues.append("Не завершены измерения. " + protocol.measurement_hint)
                result.incomplete_measurements = True
            if not instrument.serial:
                result.issues.append(f"Не найден заводской номер. Проверьте {instrument.source}.")
            _check_fields(result, failing)
            batch.append(result)

        batch_sticker = shared.get("sticker")
        if protocol.declared != 1 and batch_sticker and batch_sticker.value and (
            any(result.value("outcome") == "pass" for result in batch)
            and not any(result.value("sticker") for result in batch if result.value("outcome") == "pass")
        ):
            for result in batch:
                result.issues.append("Один номер наклейки указан на партию приборов." + location(batch_sticker))
        results.extend(batch)

    identities = Counter(result.identity for result in results if result.instrument.serial)
    stickers = Counter(result.value("sticker") for result in results
                       if result.value("outcome") == "pass" and result.value("sticker"))
    for result in results:
        if identities[result.identity] > 1:
            result.issues.append("Повтор результата для того же типа, заводского номера и даты поверки.")
        if result.value("outcome") == "pass" and stickers[result.value("sticker")] > 1:
            result.issues.append("Один номер наклейки указан у нескольких результатов.")
    return results


def _check_fields(result, failing):
    value = result.value
    if value("outcome") == "pass":
        if not valid_sticker(value("sticker")):
            result.issues.append(INVALID_STICKER + location(result.fields.get("sticker")))
        for key, title in (("sign_pass", "в паспорте"), ("sign_mi", "на приборе")):
            if type(value(key)) is not bool:
                result.issues.append(f"Укажите наличие знака {title}." + location(result.fields.get(key)))
        if value("valid_date"):
            end, start = iso_date(value("valid_date")), iso_date(value("verification_date"))
            if not end or (start and end < start):
                result.issues.append("Дата окончания должна быть корректной и не раньше даты поверки.")
    elif value("outcome") == "fail" and not value("reasons") and failing == 1:
        result.issues.append("Укажите причину непригодности." + location(result.protocol.fields.get("reasons")))
