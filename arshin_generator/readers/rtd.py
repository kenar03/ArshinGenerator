from collections import defaultdict
from datetime import date, datetime
import logging
from math import isfinite
import re

from ..dates import expiry_before_anniversary
from ..domain import Datum, ElementReading, Equipment, Instrument, Protocol
from ..normalization import DASHES, family_prefix, serial, serial_list
from ..public_registry import TYPE_NUMBER

VERSION = "rtd-v1"
DISPLAY_NAME = "ТСП/ТСМ"
PASS = "СООТВЕТСТВУЕТ"
FAIL = "НЕ СООТВЕТСТВУЕТ"
DATA_LAST_ROW = 1001
MEASUREMENT_LAST_ROW = 2034
MEASUREMENT_COLUMNS = (2, 3, 6, 7, 8, 9)
EQUIPMENT_CATEGORIES = {"си": "mis", "г/р": "mis", "эталон": "mieta", "фиф": "mieta", "фиф без №": "mieta"}


# Confirmed composition of the designation for this type. Registry lookup is independent.
MODELS_BY_TYPE = {
    "56560-14": {
        "ТСП": {"0193", "1293", "1393", "1193", "1195", "0196", "0395", "0397"},
        "ТСМ": {"0193", "1293", "1193", "1393", "0196", "0395"},
    },
}
EXPLOSION_PROTECTION = {"ex", "ех"}


def _component(value: str) -> str:
    value = re.sub(r"^[\s\-‐‑–—−]+|[\s\-‐‑–—−]+$", "", value)
    return re.sub(r"\s*([-/])\s*", r"\1", value)


def _modification(type_number: str, model: str, execution: str, ex: str) -> tuple[str, str]:
    model, execution, ex = (_component(value) for value in (model, execution, ex))
    if not model:
        return "", "Укажите модель в листе «Таблицы», ячейка A27."
    if ex and ex.casefold() not in EXPLOSION_PROTECTION:
        return "", (f"Взрывозащита «{ex}» в Таблицы!C27 не распознана. "
                    "Сверьте обозначение с паспортом СИ. Поддерживаются Ex и Ех.")
    if not TYPE_NUMBER.fullmatch(type_number):
        return "", ""
    models = MODELS_BY_TYPE.get(type_number)
    if models is None:
        return "", (f"Для типа «{type_number}» и модели «{model}» нет подтверждённого правила модификации. "
                    "Сверьте Таблицы!L27 и A27 с паспортом СИ.")
    family, _, code = family_prefix(model.translate(DASHES)).partition("-")
    if code not in models.get(family, ()):
        return "", (f"Модель «{model}» не поддерживается для типа «{type_number}». "
                    "Сверьте Таблицы!A27 и L27 с паспортом СИ.")
    # РЭД is a customer designation only for this documented execution of ТСП-0193.
    if type_number == "56560-14" and (family, code) == ("ТСП", "0193") and execution in {"17М/РЭД", "17M/РЭД"}:
        execution = execution.removesuffix("/РЭД")
    marking = model
    if execution:
        marking += execution if execution.startswith("/") else f"-{execution}"
    return (f"{marking}-{ex}" if ex else marking), ""


def confirmed_rtd_modification(type_number: str, model: str, execution: str, ex: str) -> str:
    return _modification(type_number, model, execution, ex)[0]


def _conclusion(text: str) -> str:
    if re.match(r"^(?:заключение:\s*(?:си\s+)?)?не годн(?:ы|ен|а)\b", text, re.I):
        return "fail"
    if re.match(r"^(?:заключение:\s*(?:си\s+)?)?годн(?:ы|ен|а)\b", text, re.I):
        return "pass"
    return ""


def _matrix(sheet, end: int, columns: int) -> list[tuple]:
    return [()] + list(sheet.iter_rows(max_row=end, max_col=columns, values_only=True))


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _present(value) -> str:
    text = _text(value)
    return "" if not text or re.fullmatch(r"[-–—]+", text) or text.startswith("#") else text


def matches(book) -> bool:
    required = {"Таблицы", "Отчёт", "Данные", "Списки", "Эталоны", "Оборудование"}
    if not required.issubset(book.sheetnames):
        return False
    t = _matrix(book["Таблицы"], 34, 12)
    d = _matrix(book["Данные"], 1, 9)[1]
    return (
        "Заводские номера" == _text(t[2][0])
        and "Схема соединения" == _text(t[26][7])
        and "Кол-во" == _text(t[26][9])
        and "Серийный №" == _text(t[34][0])
        and tuple(_text(d[i]) for i in (0, 1, 2, 6, 8))
        == ("Блок", "Прибор №", "ЧЭ №", "Номер с кодом", "Статус ЧЭ")
        and _text(book["Отчёт"]["A32"].value) == "Методика поверки:"
        and _text(book["Эталоны"]["G1"].value) == "Поверен до"
        and _text(book["Оборудование"]["E1"].value) == "Строка для протокола"
    )


def _condition(value, unit: str) -> str:
    text = _present(value)
    return f"{text.replace('.', ',')} {unit}" if re.fullmatch(r"[+-]?[0-9]+(?:[.,][0-9]+)?", text) else ""


def _sign(value) -> bool | None:
    if type(value) is bool:
        return value
    sign = _text(value).casefold()
    if sign in {"v", "✓", "да"}:
        return True
    if sign in {"-", "–", "—", "нет"}:
        return False
    return None


def _catalog_label(text: str) -> str:
    return " ".join(re.sub(r"\s*№\s*", " № ", text).split())


def _equipment_labels(number, name, factory):
    if number and name and factory:
        return (f"{number} {name} № {factory}", f"г/р № {number} {name} № {factory}", f"{number} {name} {factory}")
    return ()


def _equipment_item(protocol, description, source, fields, category_cells, *, modern=False):
    local_id, factory, name, kind = fields
    categories = {"си": "mis", "эталон": "mieta"} if modern else EQUIPMENT_CATEGORIES
    category = categories.get(kind, "mieta" if not modern and kind.startswith("фиф без") else "")
    type_number, registration, input_issue = "", "", ""
    if not category:
        input_issue = f"Для средства «{name or description}» не указан допустимый тип прибора. Проверьте {category_cells}."
    elif category == "mis" or TYPE_NUMBER.fullmatch(local_id):
        type_number = local_id
    else:
        registration = local_id
    if input_issue:
        protocol.issues.append(input_issue)
    return Equipment(description, source, local_id, factory, name, category=category, type_number=type_number,
                     registration=registration, input_issue=input_issue)


def _catalog_fields(row):
    number, name, factory, kind = (_text(value) for value in row[:4])
    return number, factory, name, " ".join(kind.casefold().split())


def _marked_equipment(protocol, catalog):
    result = []
    for r, row in enumerate(catalog[2:], 2):
        if _text(row[5]) != "+":
            continue
        fields = _catalog_fields(row)
        if not any(fields):
            continue
        number, factory, name, _ = fields
        description = f"{number} {name} № {factory}" if number and name and factory else name or number or f"Строка {r}"
        source = f"Оборудование!A{r}:D{r}"
        errors = [f"Оборудование!{column}{r}" for column, value in zip("ABCD", row[:4])
                  if _text(value).startswith("#")]
        if errors:
            protocol.issues.append("Ошибка Excel в реквизитах средства поверки. Проверьте " + ", ".join(errors) + ".")
        result.append(_equipment_item(protocol, description, source, fields, f"Оборудование!D{r}", modern=True))
    return result


def _legacy_equipment(protocol, t, catalog):
    lookup = defaultdict(set)
    labels = defaultdict(set)
    for r, row in enumerate(catalog[2:], 2):
        fields = _catalog_fields(row)
        entry = (*fields, r)
        if label := _present(row[4]):
            lookup[label].add(entry)
            labels[_catalog_label(label)].add(entry)
        local_id, factory, name, _ = fields
        for alias in _equipment_labels(local_id, name, factory):
            labels[_catalog_label(alias)].add(entry)
    result = []
    for r in range(10, 19):
        text = _present(t[r][0])
        if not text:
            continue
        matches = lookup.get(text) or labels.get(_catalog_label(text), set())
        identities = {entry[:4] for entry in matches}
        source = f"Таблицы!A{r}"
        if len(identities) == 1:
            catalog_rows = sorted(k for *_, k in matches)
            source += " / " + ", ".join(f"Оборудование!A{k}:E{k}" for k in catalog_rows)
            cells = ", ".join(f"Оборудование!D{k}" for k in catalog_rows)
            result.append(_equipment_item(protocol, text, source, next(iter(identities)), cells))
        else:
            result.append(Equipment(text, source))
    return result


def _extended_report_rows(report):
    # The appendix and its column titles distinguish report contents from equipment list text.
    appendix = next((r for r, row in enumerate(report[35:], 35)
                     if _text(row[0]).casefold().startswith("приложение к протоколу поверки")), 0)
    if not appendix:
        return 0, 0, 0
    results = next((r for r in range(appendix - 1, 34, -1)
                    if _text(report[r][0]).casefold().startswith("результаты поверки:")), 0)
    conclusion = next((r for r in range(results + 1, appendix)
                       if _text(report[r][0]).casefold().startswith("заключение:")), 0) if results else 0
    measurements = next((r for r in range(appendix + 1, len(report))
                         if _text(report[r][0]) == "№"
                         and _text(report[r][2]).replace(" ", "").casefold() in {"т,°с", "т,°c"}
                         and _text(report[r][3]).casefold().startswith("rэт")), 0)
    return results, conclusion, measurements - 1 if measurements else 0


def read(book, protocol: Protocol) -> None:
    protocol.measurement_hint = "Проверьте сохранённые результаты на листах «Данные» и «Таблицы»."
    data = _matrix(book["Данные"], DATA_LAST_ROW, 12)
    t = _matrix(book["Таблицы"], MEASUREMENT_LAST_ROW, 12)
    catalog = _matrix(book["Оборудование"], 301, 6)
    modern = _text(catalog[1][5]).casefold() == "используется"
    report = _matrix(book["Отчёт"], 420 if modern else 61, 10)
    results_row, conclusion_row, marking_row = _extended_report_rows(report) if modern else (0, 45, 61)
    conclusion_cell = f"Отчёт!A{conclusion_row or 336}"
    protocol.number = _present(t[1][3])
    protocol.evidence = _present(report[conclusion_row][0]) if conclusion_row else ""
    protocol.conclusion = _text(t[19][2]).casefold()
    protocol.report_outcome = _conclusion(protocol.evidence)
    protocol.conclusion_outcome = _conclusion(protocol.conclusion)
    declared = t[27][9]
    if type(declared) is int and 1 <= declared <= 1000:
        protocol.declared = declared
    else:
        protocol.issues.append("Укажите положительное целое количество приборов в Таблицы!J27.")
    if not protocol.number or "!!!!!" in protocol.number:
        protocol.issues.append("Нет номера готового протокола (Таблицы!D1). Сохраните завершённую книгу в Excel.")
    if protocol.number and protocol.number not in _text(report[20][0]):
        protocol.issues.append("Номер протокола расходится с Отчёт!A20. Сохраните книгу в Excel и загрузите снова.")
        protocol.report_outcome = ""
    scheme = _text(t[27][7]).casefold().replace("x", "х").replace("×", "х")
    if scheme in {"2", "3", "4", "2х2", "2х3", "2х4"}:
        protocol.elements = 2 if "х" in scheme else 1
    else:
        protocol.issues.append("Укажите поддерживаемую схему соединения в Таблицы!H27.")
    if protocol.declared and protocol.elements and protocol.declared * protocol.elements > DATA_LAST_ROW - 1:
        protocol.issues.append("Количество заявленных измерительных блоков превышает диапазон расчётных строк.")

    def put(key, value, source, raw=None):
        protocol.fields[key] = Datum(value, source, _text(value if raw is None else raw))

    raw_date = t[24][1]
    actual = raw_date.date() if isinstance(raw_date, datetime) else raw_date
    put("verification_date", actual.isoformat() if type(actual) is date else None, "Таблицы!B24", raw_date)
    put("type_number", _present(t[27][11]), "Таблицы!L27", t[27][11])
    kind = _text(t[1][1]).casefold()
    put("verification_type", {"первичной": "1", "первичная": "1",
                              "периодической": "2", "периодическая": "2"}.get(kind), "Таблицы!B1", kind)
    if protocol.fields["verification_type"].value == "1" and type(actual) is date:
        put("manufacture_year", f"{actual.year:04d}", "Предполагаемый год из Таблицы!B24")
    put("method", " ".join(_present(report[33][0]).split()), "Отчёт!A33", report[33][0])
    put("owner", _present(report[24][9]), "Отчёт!J24")
    put("metrologist", _present(t[23][1]), "Таблицы!B23")
    put("sticker", _present(t[20][1]), "Таблицы!B20", t[20][1])
    put("sign_mi", _sign(t[21][1]), "Таблицы!B21", t[21][1])
    put("sign_pass", _sign(t[22][1]), "Таблицы!B22", t[22][1])
    put("reasons", _present(t[20][5]), "Таблицы!F20")
    for r, key, unit in [(5, "temperature", "°C"), (6, "pressure", "кПа"),
                         (7, "humidity", "%"), (8, "other", "В")]:
        put(key, _condition(t[r][1], unit), f"Таблицы!B{r}", t[r][1])
    model, execution, ex, length = (_present(v) for v in t[27][:4])
    full = _present(report[marking_row][0]) if marking_row else ""
    put("marking", full or " / ".join(v for v in (model, execution, ex, length) if v),
        f"Отчёт!A{marking_row}" if full else "Таблицы!A27:D27")
    put("wiring", scheme, "Таблицы!H27")
    put("interval", _present(t[27][10]), "Таблицы!K27")
    component_issues = []
    for column, key, caption, raw in zip("ABC", ("model", "execution", "explosion_protection"),
                                         ("Модель", "Исполнение", "Взрывозащита"), t[27][:3]):
        put(key, _component(_present(raw)), f"Таблицы!{column}27", raw)
        if _text(raw).startswith("#"):
            component_issues.append(f"{caption} в Таблицы!{column}27 содержит ошибку Excel «{_text(raw)}».")
    interval = protocol.fields["interval"].value
    if not re.fullmatch(r"[1-9][0-9]*", interval):
        protocol.issues.append("Укажите МПИ в полных годах. Проверьте лист «Таблицы», ячейку K27.")
    elif type(actual) is date:
        try:
            if len(interval) > 4:
                raise ValueError("МПИ выводит дату окончания за допустимый диапазон дат.")
            expiry = expiry_before_anniversary(actual, int(interval))
            put("valid_date", expiry.isoformat(), "Таблицы!B24 и K27 · дата поверки + МПИ − 1 день")
        except ValueError as exc:
            protocol.issues.append(f"{exc} Проверьте дату и МПИ в Таблицы!B24 и K27.")
    put("serial_list", _present(t[19][1]), "Таблицы!B19 / D2:G4")
    put("aggregate", _present(t[19][2]), f"Таблицы!C19 / {conclusion_cell}")
    if results_row:
        put("report_results", _present(report[results_row][0]), f"Отчёт!A{results_row}")
    put("equipment_list", None, "Оборудование!F2:F301" if modern else "Таблицы!A10:A18")
    type_number = protocol.fields["type_number"].value
    if component_issues:
        modification, modification_issue = "", " ".join((*component_issues, "Исправьте ячейки и сохраните книгу."))
    else:
        modification, modification_issue = _modification(type_number, model, execution, ex)
    protocol.modification_issue = modification_issue
    if modification_issue:
        logging.warning("Модификация Excel [%s]: Таблицы!L27=%r, A27=%r, B27=%r, C27=%r. Причина: %r",
                        protocol.digest[:12], _text(t[27][11])[:128], _text(t[27][0])[:128],
                        _text(t[27][1])[:128], _text(t[27][2])[:128], modification_issue[:300])
    put("modification", modification, "Таблицы!A27:C27", " / ".join(part for part in (model, execution, ex) if part))
    if modern:
        protocol.equipment = _marked_equipment(protocol, catalog)
    else:
        protocol.equipment = _legacy_equipment(protocol, t, catalog)
        for row in range(10, 19):
            if _text(t[row][0]).startswith("#"):
                protocol.issues.append(f"Ошибка Excel в строке средства поверки Таблицы!A{row}.")
    points = 1 if _text(t[27][6]).upper() in {"C", "С"} else 2
    _instruments(protocol, t, data, points)
    if not protocol.report_outcome or not protocol.conclusion_outcome:
        protocol.issues.append(f"Не заполнено итоговое заключение. Проверьте Таблицы!C19 и {conclusion_cell}.")
    elif protocol.report_outcome != protocol.conclusion_outcome:
        protocol.issues.append(f"Итоговые заключения расходятся. Проверьте Таблицы!C19 и {conclusion_cell}.")
    elif protocol.instruments and all(item.outcome for item in protocol.instruments):
        measured = "fail" if any(item.outcome == "fail" for item in protocol.instruments) else "pass"
        if measured != protocol.report_outcome:
            protocol.issues.append(f"Итоговое заключение расходится с измерениями. Проверьте Таблицы!C19 и {conclusion_cell}.")


def _source_ranges(t, declared):
    zero = _text(t[2][8]).casefold()
    single = declared == 1 and _text(t[2][3]) and not _text(t[2][5])
    if not single and zero not in {"да", "нет"}:
        return None
    names, ranges = [], []
    width = 3 if zero == "да" else 0
    for index, row in enumerate(t[2:5]):
        start, end, suffix = row[3], row[5], _text(row[6])
        if start is None and end is None and not suffix:
            continue
        if single and index == 0:
            end = start  # The template stores the numeric part in D2 and the suffix in G2.
        if type(start) is not int or type(end) is not int or not 0 <= start <= end:
            raise ValueError("Уточните границы диапазонов заводских номеров в протоколе.")
        if len(names) + end - start + 1 > DATA_LAST_ROW - 1:
            raise ValueError("Диапазоны заводских номеров превышают вместимость протокола.")
        formatted = [f"{number:0{width}d}{suffix}" for number in range(start, end + 1)]
        ranges.append(formatted[0] if start == end else f"{formatted[0]}-{formatted[-1]}")
        names.extend(serial(number) for number in formatted)
    return (names, ", ".join(ranges)) if names else None


def _instruments(protocol: Protocol, t: list, data: list, points: int) -> None:
    by_index = defaultdict(list)
    malformed = []
    for r, row in enumerate(data[2:], 2):
        _, index, part, entry, suffix, padded, combined, _, status = row[:9]
        if entry in ("", None) and combined in ("", None):
            continue
        if type(index) is not int or type(part) is not int:
            malformed.append(r)
            continue
        raw = _text(combined)
        try:
            number = serial(raw) if isinstance(combined, str) else ""
        except ValueError:
            number = ""
        first = 35 + (r - 2) * 2
        rows = t[first:first + points]
        if not number or raw != _text(padded) + _text(suffix):
            malformed.append(r)
        values = tuple(tuple(line[c] for c in MEASUREMENT_COLUMNS) for line in rows)
        measured = len(values) == points and (points != 1 or values[0][0] == 0) and all(
            type(value) in (int, float) and isfinite(value) for line in values for value in line
        ) and all(line[-1] >= 0 for line in values)
        by_index[index].append((number, raw, ElementReading(
            part, _text(status), measured, f"Данные!I{r}, Таблицы!C{first}:J{first + points - 1}", values if measured else ()
        )))
    if malformed:
        protocol.issues.append("Некорректные или отсутствующие кэши индексов/номеров на листе «Данные»: " +
                               ", ".join(map(str, malformed[:12])) + ". Сохраните книгу в Excel.")
    if protocol.declared and set(by_index) != set(range(protocol.declared)):
        protocol.issues.append(f"Указано {protocol.declared} СИ, число или индексы групп в сохранённых строках "
                               f"не совпадают (найдено {len(by_index)}).")
    for index, parts in sorted(by_index.items()):
        numbers = {item[0] for item in parts}
        number = parts[0][0]
        elements = tuple(item[2] for item in parts)
        problem = ""
        outcome = ""
        if len(numbers) != 1 or len({item[1] for item in parts}) != 1:
            problem = "У чувствительных элементов одного прибора расходятся заводские номера."
        elif sorted(item[2].index for item in parts) != list(range(1, protocol.elements + 1)):
            problem = "Недостающий или повторный чувствительный элемент."
        else:
            mismatched = next((e for e in elements if _status_disagrees(e)), None)
            if mismatched:
                problem = f"Сохранённый результат ЧЭ расходится с допусками. Проверьте {mismatched.source}."
            elif all(e.complete and e.status in {PASS, FAIL} for e in elements):
                outcome = "fail" if any(e.status == FAIL for e in elements) else "pass"
        source = ", ".join(item.source.split(", ", 1)[0] for item in elements) + " / Таблицы!B19"
        protocol.instruments.append(Instrument(number, parts[0][1], source, elements, problem, outcome))
    try:
        source = _source_ranges(t, protocol.declared)
        raw_list = _text(t[19][1])
        if source and re.sub(r"\s+", "", raw_list.lstrip("№")) == re.sub(r"\s+", "", source[1]):
            batch = source[0]
        else:
            batch = serial_list(raw_list)
            if source and batch != source[0]:
                raise ValueError("Диапазоны и итоговый список заводских номеров в Excel различаются.")
        if protocol.declared and len(batch) != protocol.declared:
            raise ValueError(f"Указано {protocol.declared} СИ, в строке партии найдено {len(batch)} номеров.")
        if batch != [item.serial for item in protocol.instruments]:
            raise ValueError("Список партии и номера в сохранённых строках измерений различаются.")
    except ValueError as exc:
        protocol.serial_issue = f"{exc} Проверьте Таблицы!B19, D2:G4 и лист «Данные»."
        if not protocol.instruments and _present(t[19][1]):
            try:
                protocol.instruments = [Instrument(n, _text(t[19][1]), "Таблицы!B19") for n in serial_list(_text(t[19][1]))]
            except ValueError:
                pass


def _status_disagrees(reading: ElementReading) -> bool:
    if not reading.complete or reading.status not in {PASS, FAIL}:
        return False
    # Compare H/I with the saved tolerance J for each required point.
    within_limits = all(upper <= limit and lower >= -limit
                        for _, _, _, upper, lower, limit in reading.measurements)
    return within_limits != (reading.status == PASS)
