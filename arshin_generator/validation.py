from collections import Counter
import json
import re

from .domain import Datum, Protocol, Result, RegistryEntry
from .normalization import CANONICAL_NAME, metrologist, serial_list
from .registry import iso_date, resolve

RULES = "1.0.0"
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


def individual_key(protocol: Protocol, number: str) -> str:
    return f"{RULES}:{protocol.reader}:{protocol.digest}:{number}"


def protocol_key(protocol: Protocol) -> str:
    return individual_key(protocol, "")


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


def check(protocols: list[Protocol], confirmations: dict, registry: list[RegistryEntry]) -> list[Result]:
    results = []
    for protocol in protocols:
        if protocol.error or not protocol.instruments:
            continue
        shared = protocol.fields.copy()
        _merge(shared, confirmations.get(("modification", modification_key(protocol))))
        _merge(shared, confirmations.get(("protocol", protocol_key(protocol))))
        issues = protocol.issues.copy()
        if protocol.serial_issue:
            try:
                verified = serial_list(str(shared["explicit_serials"].value or ""), explicit=True)
                if verified != [item.serial for item in protocol.instruments] or len(verified) != protocol.declared:
                    raise ValueError("Подтверждённый список расходится с измерениями или количеством в Excel.")
            except (KeyError, ValueError):
                issues.append(protocol.serial_issue)
        if protocol.elements == 2 and shared.get("one_result", Datum(None, "")).value is not True:
            issues.append("Для двух ЧЭ подтвердите по описанию типа, что на физический прибор оформляется одна запись.")
        equipment = []
        when = iso_date(shared["verification_date"].value)
        for item in protocol.equipment:
            entry, error = resolve(item, registry, when)
            if entry:
                equipment.append(entry)
            else:
                issues.append(f"{error}: {item.description} ({item.source}).")
        if not protocol.equipment:
            issues.append("Средства поверки отсутствуют в Таблицы!A10:A18.")
        equipment_keys = [
            (e.category, e.type_number, e.registry_serial, e.identity_kind) if e.category == "mis"
            else (e.category, e.identifier) for e in equipment
        ]
        if len(set(equipment_keys)) != len(equipment_keys):
            issues.append("В списке средств поверки обнаружено повторное соответствие одному идентификатору.")
        for instrument in protocol.instruments:
            fields = shared.copy()
            individual = confirmations.get(("instrument", individual_key(protocol, instrument.serial)))
            if protocol.declared != 1:
                for key in ("sticker", "reasons"):
                    prior = fields.get(key, Datum("", ""))
                    fields[key] = Datum("", prior.source + " (общие сведения партии)", prior.raw)
            _merge(fields, individual)
            statuses = [e.status for e in instrument.elements]
            complete = len(statuses) == protocol.elements and all(e.complete for e in instrument.elements)
            candidate = _candidate(protocol, statuses, complete)
            result = Result(protocol, instrument, fields, equipment, issues.copy(), candidate)
            if not individual or result.value("outcome") not in {"pass", "fail"}:
                result.issues.append("Подтвердите окончательное заключение по этому СИ.")
            if instrument.problem:
                result.issues.append(instrument.problem)
            if not complete or not all(status in {PASS, FAIL} for status in statuses):
                result.issues.append("Есть неполные измерения или отсутствует сохранённый результат ЧЭ.")
            if result.value("outcome") == "pass" and (
                FAIL in statuses or protocol.conclusion.startswith("не годны")
            ):
                result.issues.append("Положительный результат противоречит сохранённым данным протокола.")
            if result.value("outcome") == "pass" and result.value("reasons"):
                result.issues.append("При положительном результате указана причина непригодности.")
            _check_fields(result, individual[0] if individual else {})
            results.append(result)
    identities = Counter((r.identity[0], r.identity[1].upper(), r.identity[2]) for r in results if r.identity[1])
    stickers = Counter(r.value("sticker") for r in results if r.value("outcome") == "pass" and r.value("sticker"))
    for result in results:
        kind, serial_number, day = result.identity
        if identities[kind, serial_number.upper(), day] > 1:
            result.issues.append("Повтор результата для того же типа, заводского номера и даты поверки.")
        if result.value("outcome") == "pass" and stickers[result.value("sticker")] > 1:
            result.issues.append("Один номер наклейки указан у нескольких результатов.")
    return results


def _check_fields(result: Result, confirmed_individual: dict) -> None:
    value = result.value
    issues = result.issues
    for key in ("type_number", "method", "temperature", "pressure", "humidity"):
        if not value(key):
            issues.append(f"Заполните поле «{LABELS[key]}».")
    if not result.instrument.serial:
        issues.append("Заводской номер нельзя подтвердить по сохранённой строке.")
    if not (iso_date(value("verification_date"))):
        issues.append("Укажите фактическую дату поверки в Таблицы!B24 и сохраните книгу в Excel.")
    if value("verification_type") not in {"1", "2"}:
        issues.append("Укажите вид поверки.")
    if not re.fullmatch(r"[А-Я]{1,3}", str(value("sign_cipher") or "")):
        issues.append("Шифр знака должен содержать 1–3 заглавные кириллические буквы А–Я.")
    if type(value("calibration")) is not bool:
        issues.append("Подтвердите, использовались ли результаты калибровки.")
    modification = str(value("modification") or "")
    if not valid_modification(result.protocol, modification):
        issues.append("Подтвердите написание модификации по официальному описанию типа.")
    try:
        original = result.fields["metrologist"]
        normalized = metrologist(str(original.value or ""))
        if not CANONICAL_NAME.fullmatch(normalized):
            raise ValueError
        result.fields["metrologist"] = Datum(normalized, original.source, original.raw or str(original.value))
    except (ValueError, KeyError):
        issues.append("Проверьте поверителя. Нужен формат «Фамилия И.О.».")
    if value("outcome") == "pass":
        sticker = str(value("sticker") or "")
        if not re.fullmatch(r"[0-9]{11}", sticker):
            issues.append("Для пригодного СИ XSD требует фактический номер наклейки из 11 цифр.")
        if any(type(confirmed_individual.get(key)) is not bool for key in ("sign_pass", "sign_mi")):
            issues.append("Подтвердите нанесение знака в паспорте и на конкретном приборе.")
        if value("valid_date"):
            end, start = iso_date(value("valid_date")), iso_date(value("verification_date"))
            if not end or (start and end < start):
                issues.append("Дата окончания должна быть корректной и не раньше даты поверки.")
            if value("valid_date_absent") is True:
                issues.append("Подтверждение отсутствия срока противоречит заполненной дате.")
        elif value("valid_date_absent") is not True:
            issues.append("Укажите дату окончания или подтвердите основание для её отсутствия.")
        elif value("verification_type") != "1":
            issues.append("Пропуск даты окончания для пригодного СИ предусмотрен для случая только первичной поверки.")
    elif value("outcome") == "fail":
        if not value("reasons"):
            issues.append("Укажите подтверждённую причину непригодности конкретного СИ.")
    if value("manufacture_year"):
        year = str(value("manufacture_year"))
        when = iso_date(value("verification_date"))
        if not re.fullmatch(r"[0-9]{4}", year) or int(year) == 0 or (when and int(year) > when.year):
            issues.append("Год выпуска должен состоять из четырёх цифр и не превышать год поверки.")
    for key, limit in {
        "type_number": 32, "modification": 128, "owner": 512, "method": 128,
        "metrologist": 128, "temperature": 128, "pressure": 128, "humidity": 128,
        "other": 128, "structure": 1024, "brief": 1024, "additional": 1024, "reasons": 1024,
    }.items():
        text = str(value(key) or "")
        if len(text) > limit:
            issues.append(f"Поле «{LABELS[key]}» превышает {limit} символов по XSD.")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", text):
            issues.append(f"Поле «{LABELS[key]}» содержит символы, недопустимые в XML.")
