from collections import Counter
import json
import re

from .confirmations import get_confirmation, individual_key, protocol_key
from .domain import Datum, Protocol, Result, RegistryEntry
from .laboratory import apply_defaults
from .normalization import CANONICAL_NAME, metrologist, serial_list
from .public_registry import TYPE_NUMBER
from .registry import has_registry_identity, iso_date, lookup_key, resolve_many

RULES = "1.5.1"
PASS = "СООТВЕТСТВУЕТ"
FAIL = "НЕ СООТВЕТСТВУЕТ"
INVALID_STICKER = "Указанный номер наклейки должен содержать не более 128 символов, допустимых в XML."
XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
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
    ("sticker", "Номер наклейки", "text"),
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
    if protocol.modification_parts and all(protocol.modification_parts[:2]):
        return json.dumps([protocol.fields["type_number"].value, *protocol.modification_parts], ensure_ascii=False)
    return legacy_modification_key(protocol)


def legacy_modification_key(protocol: Protocol) -> str:
    return json.dumps([protocol.fields["type_number"].value, protocol.fields["marking"].value], ensure_ascii=False)


def valid_modification(protocol: Protocol, value: str) -> bool:
    if not value:
        return False
    prefixes = protocol.modification_prefixes
    return not prefixes or any(value.startswith(prefix) and value[len(prefix):].strip() for prefix in prefixes)


def _merge(fields: dict, confirmation, *, preserve=()) -> None:
    if confirmation:
        data, evidence, confirmer, timestamp = confirmation
        for key, value in data.items():
            if key in preserve:
                continue
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


def valid_sticker(value) -> bool:
    number = _sticker_number(value)
    return len(number) <= 128 and not XML_FORBIDDEN.search(number)


def _check_common(protocol, fields):
    issues = []
    missing = []

    def location(key):
        source = protocol.fields.get(key, Datum("", "")).source
        sheet, _, cell = source.partition("!")
        return f" Проверьте лист «{sheet}», ячейку {cell}." if re.fullmatch(r"[A-Z]+[0-9]+", cell) else (
            f" Проверьте Excel ({source})." if source else ""
        )

    for key in ("type_number", "temperature", "pressure", "humidity"):
        if not fields.get(key, Datum("", "")).value:
            issues.append(f"Заполните поле «{LABELS[key]}»." + location(key))
            missing.append(key)
    if not fields.get("method", Datum("", "")).value:
        issues.append("Не указана методика поверки." + location("method"))
        missing.append("method")
    if not fields.get("owner", Datum("", "")).value:
        issues.append("Укажите фактического владельца СИ." + location("owner"))
        missing.append("owner")
    if not iso_date(fields["verification_date"].value):
        source = fields["verification_date"].source
        issues.append(f"Укажите фактическую дату поверки в Excel ({source}).")
    if fields.get("verification_type", Datum("", "")).value not in {"1", "2"}:
        issues.append("Укажите вид поверки." + location("verification_type"))
        missing.append("verification_type")
    if not re.fullmatch(r"[А-Я]{1,3}", str(fields.get("sign_cipher", Datum("", "")).value or "")):
        issues.append("Подтверждение: укажите шифр знака из 1–3 заглавных букв А–Я.")
        missing.append("sign_cipher")
    if type(fields.get("calibration", Datum(None, "")).value) is not bool:
        issues.append("Подтверждение: укажите, использовались ли результаты калибровки.")
        missing.append("calibration")
    if not valid_modification(protocol, str(fields.get("modification", Datum("", "")).value or "")):
        issues.append("Проверьте написание модификации по официальному описанию типа." + location("marking"))
        missing.append("modification")
    try:
        original = fields["metrologist"]
        normalized = metrologist(str(original.value or ""))
        if not CANONICAL_NAME.fullmatch(normalized):
            raise ValueError
        fields["metrologist"] = Datum(normalized, original.source, original.raw or str(original.value))
    except (ValueError, KeyError):
        issues.append("Проверьте поверителя в формате «Фамилия И.О.»." + location("metrologist"))
        missing.append("metrologist")
    return issues, missing


def check(protocols: list[Protocol], confirmations: dict, registry: list[RegistryEntry],
          lookups: dict | None = None, *, laboratory=None, check_registry=True,
          require_public=False, type_issues=None) -> list[Result]:
    results = []
    lookups = lookups or {}
    for protocol in protocols:
        if protocol.error or not protocol.instruments:
            continue
        shared = protocol.fields.copy()
        if not shared.get("modification", Datum("", "")).value:
            _merge(shared, confirmations.get(("modification", modification_key(protocol))) or
                   confirmations.get(("modification", legacy_modification_key(protocol))))
        common = get_confirmation(confirmations, "protocol", protocol)
        known = {key for key, datum in shared.items() if datum.value is not None and datum.value != ""}
        # A supplied but unusable field can still be corrected against the underlying documents.
        if not require_public and "metrologist" in known:
            try:
                metrologist(str(shared["metrologist"].value))
            except ValueError:
                known.remove("metrologist")
        _merge(shared, common, preserve=known)
        workbook_date = protocol.fields.get("valid_date", Datum(None, "")).value
        date_issues, warnings = apply_defaults(shared, laboratory or {}, workbook_date=workbook_date,
            expiry_exception=get_confirmation(confirmations, "expiry_exception", protocol))
        issues = [*protocol.issues, *date_issues]
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
            if not check_registry:
                if require_public and not has_registry_identity(item):
                    equipment_issues.append(f"Уточните реестровый и заводской номера средства «{item.name or item.description}»"
                                            f" в Excel ({item.source}).")
                continue
            online = lookups.get(key)
            entries, error = resolve_many(item, registry, when, online, require_public=require_public)
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
        number = str(shared.get("type_number", Datum("", "")).value or "")
        if number and not TYPE_NUMBER.fullmatch(number):
            common_issues.append("Проверьте регистрационный номер типа СИ в Excel.")
        if check_registry and type_issues and number in type_issues:
            common_issues.append(type_issues[number])
        issues.extend(common_issues)
        missing.extend(common_fields)
        evidence = [_individual_evidence(protocol, instrument) for instrument in protocol.instruments]
        auto = _report_agrees(protocol, evidence, shared)
        if require_public and not auto and (not protocol.report_outcome or not protocol.conclusion_outcome or
                protocol.report_outcome != protocol.conclusion_outcome or
                (protocol.report_outcome == "pass" and any(FAIL in statuses for statuses, _ in evidence))):
            issues.append("Заключение не согласуется с результатами измерений. Проверьте исходный Excel.")
        shared_issues = tuple(dict.fromkeys(issues + equipment_issues))
        batch_start = len(results)
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
            preserve = {"outcome", "reasons"} if auto else set()
            if require_public:
                # Legacy per-instrument values cannot override the workbook or a documented expiry rule.
                preserve.update(key for key in ("sign_pass", "sign_mi", "modification")
                                if key in protocol.fields and protocol.fields[key].value not in (None, ""))
                if "valid_date" in fields and fields["valid_date"].value:
                    preserve.update(("valid_date", "valid_date_absent"))
            _merge(fields, individual, preserve=preserve)
            candidate = _candidate(protocol, statuses, complete)
            result = Result(protocol, instrument, fields, equipment, candidate=candidate, shared_issues=shared_issues,
                            equipment_issues=tuple(equipment_issues), missing_fields=tuple(missing), warnings=warnings)
            if result.value("outcome") not in {"pass", "fail"}:
                result.issues.append("Результат измерений не позволяет определить окончательное заключение.")
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
            result.measurement_issues = tuple(result.issues)
            _check_fields(result)
            results.append(result)
        batch_sticker = _sticker_number(shared.get("sticker", Datum("", "")).value)
        if protocol.declared != 1 and batch_sticker:
            batch = results[batch_start:]
            applicable = [result for result in batch if result.value("outcome") == "pass"]
            if applicable and not valid_sticker(batch_sticker):
                for result in batch:
                    result.issues.append("Проверьте номер наклейки на листе «Таблицы». " + INVALID_STICKER)
            elif applicable and not any(_sticker_number(r.value("sticker")) == batch_sticker for r in applicable):
                for result in batch:
                    result.issues.append("В протоколе указан один номер наклейки на партию. "
                                         "Уточните, какому прибору он принадлежит.")
                    if result.value("outcome") == "pass":
                        result.repair_fields.add("sticker")
    identities = Counter((r.identity[0], r.identity[1].upper(), r.identity[2]) for r in results if r.identity[1])
    stickers = Counter(number for r in results if r.value("outcome") == "pass"
                       if (number := _sticker_number(r.value("sticker"))))
    for result in results:
        kind, serial_number, day = result.identity
        if identities[kind, serial_number.upper(), day] > 1:
            result.issues.append("Повтор результата для того же типа, заводского номера и даты поверки.")
        if result.value("outcome") == "pass" and stickers[_sticker_number(result.value("sticker"))] > 1:
            result.issues.append("Один номер наклейки указан у нескольких результатов.")
            result.repair_fields.add("sticker")
    return results


def _sticker_number(value) -> str:
    return str(value or "").strip()


def _check_fields(result: Result) -> None:
    value = result.value
    issues = result.issues
    if not result.instrument.serial:
        issues.append("Заводской номер нельзя подтвердить по сохранённой строке.")
    if value("outcome") == "pass":
        if not valid_sticker(value("sticker")):
            issues.append(INVALID_STICKER)
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
        if XML_FORBIDDEN.search(text):
            issues.append(f"Поле «{LABELS[key]}» содержит символы, недопустимые в XML.")
            result.repair_fields.add(key)
