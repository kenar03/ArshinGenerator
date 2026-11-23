import re
from collections import Counter

UPPER_CYR = "АВЕКМНОРСТУХ"
UPPER_LAT = "ABEKMHOPCTYX"
VISUAL = str.maketrans(UPPER_CYR + UPPER_CYR.lower(), UPPER_LAT + UPPER_LAT.lower())
DASHES = str.maketrans("–—−‑", "----")


def serial(value: str) -> str:
    normalized = value.strip().translate(DASHES).translate(VISUAL)
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[ ./-][A-Za-z0-9]+)*", normalized or "") or len(normalized) > 128:
        raise ValueError(f"Уточните заводской номер «{value}»: проверьте цифры, латинские буквы и разделители.")
    return normalized


def _range(token: str, endpoints: tuple[str, str], limit: int) -> list[str]:
    left, right = endpoints
    a = re.fullmatch(r"([0-9]+)([A-Za-z][A-Za-z0-9]*)?", left)
    b = re.fullmatch(r"([0-9]+)([A-Za-z][A-Za-z0-9]*)?", right)
    if not a or not b:
        raise ValueError(f"Уточните, является ли «{token}» диапазоном или составным номером.")
    first, first_suffix = a.groups()
    last, last_suffix = b.groups()
    count = int(last) - int(first) + 1
    if (
        (first_suffix and first_suffix != last_suffix)
        or max(len(first), len(last)) > 6
        or (len(first) != len(last) and (first.startswith("0") or last.startswith("0")))
        or not 1 <= count <= limit
    ):
        raise ValueError(f"Диапазон «{token}» неоднозначен. Введите точный список заводских номеров.")
    width = len(first) if len(first) == len(last) else 0
    return [f"{i:0{width}d}{last_suffix or ''}" for i in range(int(first), int(last) + 1)]


def _unique(values: list[str]) -> None:
    repeated = [v for v, n in Counter(value.upper() for value in values).items() if n > 1]
    if repeated:
        raise ValueError("Повтор заводского номера после проверки алфавита: " + ", ".join(repeated))


def serial_list(raw: str, limit: int = 1000, *, explicit: bool = False) -> list[str]:
    text = raw.strip().removeprefix("№").strip().translate(DASHES)
    if not explicit:
        text = re.sub(r"\s*-\s*", "-", text)
    tokens = [part.strip() for part in re.split(r"[,;\r\n]+", text) if part.strip()] if explicit else re.findall(r"[^\s,;]+", text)
    if not tokens or "..." in text or "…" in text:
        raise ValueError("Список заводских номеров пуст или содержит неполное перечисление.")
    values = []
    for token in tokens:
        token = token.removeprefix("№")
        # A multi-hyphen number is a single identifier. A single dash is ambiguous
        # unless both ends satisfy the supported range rule or the list is explicit.
        if not explicit and token.count("-") == 1:
            parts = tuple(serial(part) for part in token.split("-"))
            values.extend(_range(token, parts, limit))
        else:
            values.append(serial(token))
        if len(values) > limit:
            raise ValueError("Список превышает допустимую вместимость протокола.")
    _unique(values)
    return values


_NAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)*"
_INITIAL = r"([А-ЯЁ])\.\s*([А-ЯЁ])\."
CANONICAL_NAME = re.compile(rf"{_NAME} [А-ЯЁ]\.[А-ЯЁ]\.")


def metrologist(value: str) -> str:
    value = " ".join(value.split())
    forward = re.fullmatch(rf"({_NAME})\s+{_INITIAL}", value)
    reverse = re.fullmatch(rf"{_INITIAL}\s*({_NAME})", value)
    full = re.fullmatch(rf"({_NAME}) ({_NAME}) ({_NAME})", value)
    if forward:
        surname, first, middle = forward.groups()
    elif reverse:
        first, middle, surname = reverse.groups()
    elif full:
        surname, first, middle = full.groups()
    else:
        raise ValueError("Укажите фактического поверителя в формате «Фамилия И.О.».")
    return f"{surname} {first[0]}.{middle[0]}."


def family_prefix(value: str) -> str:
    aliases = {"TSP": "ТСП", "TCM": "ТСМ", "TSM": "ТСМ", "ТСП": "ТСП", "ТСМ": "ТСМ"}
    return re.sub(
        r"^(?:TSP|TCM|TSM|ТСП|ТСМ)(?=-)", lambda m: aliases[m[0].upper()], value.strip(), flags=re.I
    )


def modification_candidate(marking: str) -> str:
    text = family_prefix(marking.translate(DASHES))
    match = re.fullmatch(
        r"((?:ТСП|ТСМ)-0193-[0-9]{2}[МMБ]?)(/РЭД)?(-Ex)?"
        r"(?:-[0-9]+(?:\([0-9]+(?:/[0-9]+)?\))?)?", text
    )
    if match:
        return match[1] + (match[3] or "")
    return text


def structured_candidate(model: str, version: str, ex: str) -> str:
    if not model or not version:
        return ""
    separator = "" if version.startswith(("/", "-")) else "-"
    suffix = ex.strip("-")
    return modification_candidate(f"{model}{separator}{version}" + (f"-{suffix}" if suffix else ""))
