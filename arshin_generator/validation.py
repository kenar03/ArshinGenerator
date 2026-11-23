from collections import Counter
import json
import re

from .confirmations import get_confirmation, individual_key, protocol_key
from .domain import Datum, Protocol, Result, RegistryEntry
from .normalization import CANONICAL_NAME, metrologist, serial_list
from .registry import iso_date, lookup_key, resolve_many

RULES = "1.2.0"
PASS = "СООТВЕТСТВУЕТ"
FAIL = "НЕ СООТВЕТСТВУЕТ"
SHARED_FIELDS = (
    ("verification_type", "Вид поверки", "choice"),
    ("sign_cipher", "Шифр знака", "text"),
    ("calibration", "Использована калибровка", "bool"),
    ("metrologist", "Поверитель", "text"),
    ("owner", "Владелец СИ", "text"),
    ("valid_date", "Действительна до", "date"),
    ("valid_date_absent", "Срок не указывается по документу", "bool"),
    ("sign_pass", "Знак в паспортах всей партии", "bool"),
    ("sign_mi", "Знак на приборах всей партии", "bool"),
    ("temperature", "Температура", "text"),
    ("pressure", "Атмосферное давление", "text"),
    ("humidity", "Относительная влажность", "text"),
    ("other", "Другие факторы", "text"),
    ("structure", "Состав СИ", "text"),
    ("brief", "Характеристика сокращённого объёма", "text"),
    ("additional", "Прочие сведения", "text"),
    ("one_result", "Одна запись на СИ с двумя ЧЭ подтверждена", "bool"),
    ("explicit_serials", "Точный список номеров при неоднозначности", "list"),
)
INDIVIDUAL_FIELDS = (
    ("outcome", "Окончательное заключение", "outcome"),
    ("reasons", "Причина непригодности", "text"),
    ("sticker", "Номер наклейки (11 цифр)", "text"),
    ("sign_pass", "Знак в паспорте", "bool"),
    ("sign_mi", "Знак на приборе", "bool"),
    ("manufacture_year", "Год выпуска", "text"),
)
LABELS = {
    **{k: title for k, title, _ in SHARED_FIELDS + INDIVIDUAL_FIELDS},
    "type_number": "Регистрационный номер типа",
    "verification_date": "Дата поверки",
    "method": "Методика поверки",
    "marking": "Полная маркировка",
    "modification": "Модификация по описанию типа",
    "wiring": "Схема соединения",
    "interval": "Межповерочный интервал",
    "aggregate": "Общее заключение протокола",
    "serial_list": "Список партии",
    "selected_standard": "Эталон в расчёте",
    "review": "Подтверждение результата",
}


def modification_key(protocol: Protocol) -> str:
    return json.dumps([protocol.fields["type_number"].value, protocol.fields["marking"].value], ensure_ascii=False)


def valid_modification(protocol: Protocol, value: str) -> bool:
    if not value:
        return False
    prefixes = protocol.modification_prefixes
    return not prefixes or any(value.startswith(prefix) and value[len(prefix):].strip() for prefix in prefixes)


def _merge(fields: dict, confirmation) -> None:
    if confirmation:
        data, evidence, confirmer, timestamp = confirmation
        for key, value in data.items():
            raw = fields[key].raw if key in fields else ""
            fields[key] = Datum(value, f"Подтверждено: {confirmer}, {timestamp}. {evidence}", raw)


def _candidate(protocol: Protocol, statuses: list[str], complete: bool) -> str:
    if not complete or not statuses or not all(status in {PASS, FAIL} for status in statuses):
        return ""
    if FAIL in statuses:
        return "fail"
    return "pass" if protocol.conclusion.startswith("годны к применению") else ""


def _individual_evidence(protocol, instrument):
    statuses = [e.status for e in instrument.elements]
    complete = (bool(statuses) and not instrument.problem and len(statuses) == protocol.elements and
                all(e.complete and e.status in {PASS, FAIL} for e in instrument.elements))
    return statuses, complete


def _report_agrees(protocol, evidence, shared):
    if (protocol.serial_issue or not protocol.report_outcome or not protocol.number or "!!!!!" in protocol.number or
            protocol.report_outcome != protocol.conclusion_outcome or
            not iso_date(shared["verification_date"].value) or protocol.declared != len(evidence) or
            not all(complete for _, complete in evidence)):
        return False
    reasons = shared.get("reasons", Datum("", "")).value
    failing = [FAIL in statuses for statuses, _ in evidence]
    if protocol.report_outcome == "pass":
        return not any(failing) and not reasons
    return protocol.declared == 1 and all(failing) and bool(reasons)


def _check_common(protocol, fields):
    issues = []
    missing = []
    for key in ("type_number", "method", "temperature", "pressure", "humidity"):
        if not fields.get(key, Datum("", "")).value:
            issues.append(f"Заполните поле «{LABELS[key]}».")
            missing.append(key)
    if not iso_date(fields["verification_date"].value):
        issues.append("Ошибка: укажите фактическую дату поверки в Таблицы!B24 и сохраните книгу в Excel.")
    if fields.get("verification_type", Datum("", "")).value not in {"1", "2"}:
        issues.append("Подтверждение: укажите вид поверки.")
        missing.append("verification_type")
    if not re.fullmatch(r"[А-Я]{1,3}", str(fields.get("sign_cipher", Datum("", "")).value or "")):
        issues.append("Подтверждение: укажите шифр знака из 1–3 заглавных букв А–Я.")
        missing.append("sign_cipher")
    if type(fields.get("calibration", Datum(None, "")).value) is not bool:
        issues.append("Подтверждение: укажите, использовались ли результаты калибровки.")
        missing.append("calibration")
    if not valid_modification(protocol, str(fields.get("modification", Datum("", "")).value or "")):
        issues.append("Подтверждение: проверьте написание модификации по официальному описанию типа.")
        missing.append("modification")
    try:
        original = fields["metrologist"]
        normalized = metrologist(str(original.value or ""))
        if not CANONICAL_NAME.fullmatch(normalized):
            raise ValueError
        fields["metrologist"] = Datum(normalized, original.source, original.raw or str(original.value))
    except (ValueError, KeyError):
        issues.append("Подтверждение: проверьте поверителя в формате «Фамилия И.О.».")
        missing.append("metrologist")
    return issues, missing


def check(protocols: list[Protocol], confirmations: dict, registry: list[RegistryEntry],
          lookups: dict | None = None) -> list[Result]:
    results = []
    lookups = lookups or {}
    for protocol in protocols:
        if protocol.error or not protocol.instruments:
            continue
        shared = protocol.fields.copy()
        _merge(shared, confirmations.get(("modification", modification_key(protocol))))
        common = get_confirmation(confirmations, "protocol", protocol)
        if protocol.declared != 1:
            for key in ("sign_pass", "sign_mi"):
                item = shared.get(key, Datum(None, ""))
                shared[key] = Datum(None, item.source + " (применение ко всей партии уточняется)", item.raw)
        _merge(shared, common)
        issues = protocol.issues.copy()
        missing = []
        if protocol.serial_issue:
            try:
                verified = serial_list(str(shared["explicit_serials"].value or ""), explicit=True)
                if verified != [item.serial for item in protocol.instruments] or len(verified) != protocol.declared:
                    raise ValueError("Подтверждённый список расходится с измерениями или количеством в Excel.")
            except (KeyError, ValueError):
                issues.append(protocol.serial_issue)
                missing.append("explicit_serials")
        if protocol.elements == 2 and shared.get("one_result", Datum(None, "")).value is not True:
            issues.append("Для двух ЧЭ подтвердите по описанию типа, что на физический прибор оформляется одна запись.")
            missing.append("one_result")
        equipment = []
        equipment_issues = []
        seen_equipment_issues = set()
        when = iso_date(shared["verification_date"].value)
        for item in protocol.equipment:
            key = lookup_key(item, when.isoformat() if when else "")
            online = lookups.get(key)
            entries, error = resolve_many(item, registry, when, online)
            if entries:
                equipment.extend(entries)
            elif (key, error) not in seen_equipment_issues or not (item.source_id and item.serial):
                equipment_issues.append(f"{error} · {item.name or item.description}.")
                seen_equipment_issues.add((key, error))
        if not protocol.equipment:
            equipment_issues.append("В протоколе не указаны средства поверки.")
        equipment_keys = [
            (e.category, e.type_number, e.registry_serial, e.identity_kind) if e.category == "mis"
            else (e.category, e.identifier) for e in equipment
        ]
        if len(set(equipment_keys)) != len(equipment_keys):
            equipment_issues.append("В списке средств поверки обнаружено повторное соответствие одному идентификатору.")
        common_issues, common_fields = _check_common(protocol, shared)
        issues.extend(common_issues)
        missing.extend(common_fields)
        evidence = [_individual_evidence(protocol, instrument) for instrument in protocol.instruments]
        auto = _report_agrees(protocol, evidence, shared)
        shared_issues = tuple(dict.fromkeys(issues + equipment_issues))
        for instrument, (statuses, complete) in zip(protocol.instruments, evidence):
            fields = shared.copy()
            individual = get_confirmation(confirmations, "instrument", protocol, instrument)
            if protocol.declared != 1:
                for key in ("sticker", "reasons"):
                    prior = fields.get(key, Datum("", ""))
                    fields[key] = Datum("", prior.source + " (общие сведения партии)", prior.raw)
            if auto:
                evidence_source = "Согласованы заключение отчёта Excel и сохранённые результаты всех ЧЭ"
                fields["outcome"] = Datum(protocol.report_outcome, evidence_source, protocol.evidence)
            _merge(fields, individual)
            candidate = _candidate(protocol, statuses, complete)
            result = Result(protocol, instrument, fields, equipment, candidate=candidate, shared_issues=shared_issues,
                            equipment_issues=tuple(equipment_issues), missing_fields=tuple(missing))
            if result.value("outcome") not in {"pass", "fail"}:
                result.issues.append("Подтверждение: проверьте окончательное заключение по этому СИ.")
                result.repair_fields.add("outcome")
            if instrument.problem:
                result.issues.append(instrument.problem)
            if not complete or not all(status in {PASS, FAIL} for status in statuses):
                result.issues.append("Есть неполные измерения или отсутствует сохранённый результат ЧЭ.")
            if result.value("outcome") == "pass" and (
                FAIL in statuses or "fail" in {protocol.report_outcome, protocol.conclusion_outcome}
            ):
                result.issues.append("Положительный результат противоречит сохранённым данным протокола.")
                result.repair_fields.add("outcome")
            if result.value("outcome") == "pass" and result.value("reasons"):
                result.issues.append("При положительном результате указана причина непригодности.")
                result.repair_fields.update(("outcome", "reasons"))
            _check_fields(result)
            results.append(result)
    identities = Counter((r.identity[0], r.identity[1].upper(), r.identity[2]) for r in results if r.identity[1])
    stickers = Counter(r.value("sticker") for r in results if r.value("outcome") == "pass" and r.value("sticker"))
    for result in results:
        kind, serial_number, day = result.identity
        if identities[kind, serial_number.upper(), day] > 1:
            result.issues.append("Повтор результата для того же типа, заводского номера и даты поверки.")
        if result.value("outcome") == "pass" and stickers[result.value("sticker")] > 1:
            result.issues.append("Один номер наклейки указан у нескольких результатов.")
            result.repair_fields.add("sticker")
    return results


def _check_fields(result: Result) -> None:
    value = result.value
    issues = result.issues
    if not result.instrument.serial:
        issues.append("Заводской номер нельзя подтвердить по сохранённой строке.")
    if value("outcome") == "pass":
        sticker = str(value("sticker") or "")
        if not re.fullmatch(r"[0-9]{11}", sticker):
            issues.append("Наклейка: пакетная XSD требует фактический номер из 11 цифр для пригодного СИ.")
            result.repair_fields.add("sticker")
        if any(type(value(key)) is not bool for key in ("sign_pass", "sign_mi")):
            issues.append("Подтверждение: укажите знаки на приборе и в паспорте.")
            result.repair_fields.update(key for key in ("sign_pass", "sign_mi") if type(value(key)) is not bool)
        if value("valid_date"):
            end, start = iso_date(value("valid_date")), iso_date(value("verification_date"))
            if not end or (start and end < start):
                issues.append("Дата окончания должна быть корректной и не раньше даты поверки.")
                result.repair_fields.add("valid_date")
            if value("valid_date_absent") is True:
                issues.append("Подтверждение отсутствия срока противоречит заполненной дате.")
                result.repair_fields.update(("valid_date", "valid_date_absent"))
        elif value("valid_date_absent") is not True:
            issues.append("Укажите дату окончания или подтвердите основание для её отсутствия.")
            result.repair_fields.update(("valid_date", "valid_date_absent"))
        elif value("verification_type") != "1":
            issues.append("Пропуск даты окончания для пригодного СИ предусмотрен для случая только первичной поверки.")
            result.repair_fields.update(("valid_date", "valid_date_absent"))
    elif value("outcome") == "fail":
        if not value("reasons"):
            issues.append("Укажите подтверждённую причину непригодности конкретного СИ.")
            result.repair_fields.add("reasons")
    if value("manufacture_year"):
        year = str(value("manufacture_year"))
        when = iso_date(value("verification_date"))
        if not re.fullmatch(r"[0-9]{4}", year) or int(year) == 0 or (when and int(year) > when.year):
            issues.append("Год выпуска должен состоять из четырёх цифр и не превышать год поверки.")
            result.repair_fields.add("manufacture_year")
    for key, limit in {
        "type_number": 32, "modification": 128, "owner": 512, "method": 128,
        "metrologist": 128, "temperature": 128, "pressure": 128, "humidity": 128,
        "other": 128, "structure": 1024, "brief": 1024, "additional": 1024, "reasons": 1024,
    }.items():
        text = str(value(key) or "")
        if len(text) > limit:
            issues.append(f"Поле «{LABELS[key]}» превышает {limit} символов по XSD.")
            result.repair_fields.add(key)
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", text):
            issues.append(f"Поле «{LABELS[key]}» содержит символы, недопустимые в XML.")
            result.repair_fields.add(key)
