from datetime import timedelta
import re

from .domain import Datum
from .public_registry import TYPE_NUMBER
from .registry import iso_date


SETTING = "laboratory"
DEFAULT_FIELDS = (
    ("sign_cipher", "Шифр знака поверки", "text"),
    ("calibration", "Использование результатов калибровки", "bool"),
    ("verification_type", "Вид поверки при пустом поле Excel", "choice"),
)


def validate_defaults(values):
    cipher = values.get("sign_cipher")
    if cipher and not re.fullmatch(r"[А-Я]{1,3}", cipher):
        raise ValueError("Шифр должен содержать от одной до трёх заглавных русских букв.")
    if values.get("verification_type") not in {None, "", "1", "2"}:
        raise ValueError("Выберите вид поверки.")
    if values.get("calibration") is not None and type(values["calibration"]) is not bool:
        raise ValueError("Уточните использование результатов калибровки.")


def expiry_rule(values):
    number, start, end = (values[key] for key in ("type_number", "from_date", "until_date"))
    first, last = iso_date(start), iso_date(end)
    if not TYPE_NUMBER.fullmatch(number) or not first or (end and (not last or last < first)):
        raise ValueError("Укажите регистрационный номер типа и корректный период применения правила.")
    years = values["years"]
    if not re.fullmatch(r"[1-9][0-9]?", str(years)) or not values["document"].strip():
        raise ValueError("Укажите срок в полных годах и документ, подтверждающий правило для этого типа СИ.")
    return {**values, "years": int(years)}


def apply_defaults(fields, settings, *, workbook_date=None, expiry_exception=None):
    def value(key):
        return fields.get(key, Datum(None, ""))

    def vacant(key):
        item = value(key)
        return item.value in (None, "") and item.raw.strip() in {"", "-", "–", "—"}

    for key, _, _ in DEFAULT_FIELDS:
        setting = settings.get(key)
        if vacant(key) and setting not in (None, ""):
            fields[key] = Datum(setting, "Подтверждённая настройка лаборатории")
    day = iso_date(value("verification_date").value)
    if expiry_exception and day:
        exception_fields, basis, _, _ = expiry_exception
        end = iso_date(exception_fields.get("valid_date"))
        if end and end >= day and basis.strip():
            fields["valid_date"] = Datum(end.isoformat(), f"Исключение по документу · {basis}")
            fields["valid_date_absent"] = Datum(False, fields["valid_date"].source)
            return (), (f"Особый срок до {end:%d.%m.%Y}. Основание · {basis}",)
    rule = settings.get("expiry_rules", {}).get(value("type_number").value)
    if not day or not rule or value("calibration").value is not False:
        return (), ()
    first, last = iso_date(rule["from_date"]), iso_date(rule["until_date"])
    years = rule["years"]
    if not first or day < first or (last and day > last) or value("interval").value != str(years):
        return (), ()
    try:
        expiry = day.replace(year=day.year + years) - timedelta(days=1)
    except ValueError:
        # A leap-day anniversary needs the laboratory's explicit date.
        return ("Для этой даты нельзя однозначно определить годовщину. Укажите срок по документу.",), ()
    if workbook_date and workbook_date != expiry.isoformat():
        return (f"Дата окончания в Excel расходится с расчётом ({expiry:%d.%m.%Y}). "
                "Исправьте протокол или укажите документ для исключения.",), ()
    previous = value("valid_date").value
    had_absence = value("valid_date_absent").value is True
    fields["valid_date"] = Datum(expiry.isoformat(), f"Правило лаборатории · {rule['document']}")
    fields["valid_date_absent"] = Datum(False, fields["valid_date"].source)
    if (previous and previous != expiry.isoformat()) or had_absence:
        return (), (f"Дата окончания пересчитана по подтверждённому МПИ: {expiry:%d.%m.%Y}.",)
    return (), ()
