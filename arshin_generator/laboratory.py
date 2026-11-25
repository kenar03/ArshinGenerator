import re

from .domain import Datum

SETTING = "laboratory"
LABORATORY_DEFAULTS = {"sign_cipher": "ВЦЯ", "calibration": False}
DEFAULT_FIELDS = (
    ("sign_cipher", "Условный шифр знака поверки", "text"),
    ("calibration", "Использование результатов калибровки", "bool"),
)


def validate_defaults(values):
    cipher = values.get("sign_cipher")
    if cipher and not re.fullmatch(r"[А-Я]{1,3}", cipher):
        raise ValueError("Шифр должен содержать от одной до трёх заглавных русских букв.")
    if values.get("calibration") is not None and type(values["calibration"]) is not bool:
        raise ValueError("Уточните использование результатов калибровки.")


def apply_defaults(fields, settings, *, source="Настройки лаборатории"):
    for key, _, _ in DEFAULT_FIELDS:
        current = fields.get(key, Datum(None, ""))
        setting = settings.get(key)
        if current.value in (None, "") and current.raw.strip() in {"", "-", "–", "—"} and setting not in (None, ""):
            fields[key] = Datum(setting, source)
