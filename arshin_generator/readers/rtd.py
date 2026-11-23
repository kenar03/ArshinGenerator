from collections import defaultdict
from datetime import date, datetime
from math import isfinite
import re

from ..domain import Datum, ElementReading, Equipment, Instrument, Protocol
from ..normalization import serial, serial_list, structured_candidate

VERSION = "rtd-v1"
PASS = "СООТВЕТСТВУЕТ"
FAIL = "НЕ СООТВЕТСТВУЕТ"
DATA_LAST_ROW = 1001
MEASUREMENT_LAST_ROW = 2034
MEASUREMENT_COLUMNS = (2, 3, 6, 7, 8, 9)


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
    if _text(value) in {"V", "✓", "Да"}:
        return True
    if _text(value) in {"-", "Нет"}:
        return False
    return None


def _equipment(t, catalog, etalons) -> list[Equipment]:
    lookup = defaultdict(set)
    for r, row in enumerate(catalog[2:], 2):
        if _present(row[4]):
            lookup[_text(row[4])].add((_text(row[0]), _text(row[2]), _text(row[1]), _text(row[3]), r))
    result = []
    for r in range(10, 19):
        text = _present(t[r][0])
        if not text:
            continue
        matches = lookup[text]
        identities = {entry[:4] for entry in matches}
        source = f"Таблицы!A{r}"
        category, type_number, registration = "", "", ""
        if len(identities) == 1:
            local_id, factory, name, registry_format = next(iter(identities))
            source += " / " + ", ".join(f"Оборудование!A{k}:E{k}" for *_, k in sorted(matches))
            if registry_format.casefold() == "г/р":
                category, type_number = "mis", local_id
            elif registry_format.casefold() == "фиф" or registry_format.casefold().startswith("фиф без"):
                category = "mieta"
                if re.fullmatch(r"[0-9]+-[0-9]+", local_id):
                    type_number = local_id
                elif re.fullmatch(r"[0-9]+\.[0-9]+\.[\w]+\.[0-9]+", local_id):
                    registration = local_id
                else:
                    category = ""
        else:
            local_id, name, factory = "", "", ""
        hints = {
            f"Эталоны!F{k}:G{k}: {_text(row[5])}, до {_text(row[6])}"
            for k, row in enumerate(etalons[2:], 2)
            if factory and _text(row[0]) == _text(t[30][7]) and _text(row[0]).endswith(f"№{factory}")
        }
        result.append(Equipment(text, source, local_id, factory, name, "; ".join(sorted(hints)),
                                category, type_number, registration))
    return result


def read(book, protocol: Protocol) -> None:
    protocol.reader = VERSION
    protocol.modification_prefixes = ("ТСП-", "ТСМ-")
    data = _matrix(book["Данные"], DATA_LAST_ROW, 12)
    t = _matrix(book["Таблицы"], MEASUREMENT_LAST_ROW, 12)
    report = _matrix(book["Отчёт"], 61, 10)
    protocol.number = _present(t[1][3])
    protocol.evidence = _present(report[45][0])
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
        protocol.issues.append(f"Схема «{scheme}» не подтверждена для этой версии шаблона (Таблицы!H27).")
    print_limit = data[1][11]
    if protocol.declared and protocol.elements and type(print_limit) is int and print_limit > 0:
        blocks = protocol.declared * protocol.elements
        if blocks > print_limit:
            protocol.notices.append(f"Печатная вместимость «Данные»!L1 — {print_limit} блоков, в партии {blocks}. "
                                    "Проверьте полный отчёт. Выгрузка XML проверяет все строки измерений.")
    if protocol.declared and protocol.elements and protocol.declared * protocol.elements > DATA_LAST_ROW - 1:
        protocol.issues.append("Количество заявленных измерительных блоков превышает диапазон расчётных строк.")

    def put(key, value, source, raw=None):
        protocol.fields[key] = Datum(value, source, _text(value if raw is None else raw))

    raw_date = t[24][1]
    actual = raw_date.date() if isinstance(raw_date, datetime) else raw_date
    put("verification_date", actual.isoformat() if type(actual) is date else None, "Таблицы!B24", raw_date)
    put("type_number", _present(t[27][11]), "Таблицы!L27")
    kind = _text(t[1][1]).casefold()
    put("verification_type", {"первичной": "1", "первичная": "1",
                              "периодической": "2", "периодическая": "2"}.get(kind), "Таблицы!B1", kind)
    put("method", _present(report[33][0]), "Отчёт!A33")
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
    full = _present(report[61][0])
    put("marking", full or " / ".join(v for v in (model, execution, ex, length) if v),
        "Отчёт!A61 / Таблицы!A27:D27")
    put("wiring", scheme, "Таблицы!H27")
    put("interval", _present(t[27][10]), "Таблицы!K27")
    put("serial_list", _present(t[19][1]), "Таблицы!B19 / D2:G4")
    put("aggregate", _present(t[19][2]), "Таблицы!C19 / Отчёт!A45")
    put("selected_standard", _present(t[30][7]), "Таблицы!H30:J30")
    protocol.proposed_modification = structured_candidate(model, execution, ex)
    protocol.equipment = _equipment(t, _matrix(book["Оборудование"], 301, 5), _matrix(book["Эталоны"], 301, 7))
    for row in range(10, 19):
        if _text(t[row][0]).startswith("#"):
            protocol.issues.append(f"Ошибка Excel в строке средства поверки Таблицы!A{row}.")
    standard = _present(t[30][7])
    model_text, _, factory = standard.rpartition("№")
    if standard and not any(factory.strip() == item.serial and model_text.strip() and
                            model_text.strip().casefold() in item.description.casefold() for item in protocol.equipment):
        protocol.issues.append("Выбранный эталон из Таблицы!H30 не идентифицирован среди средств поверки A10:A18.")
    _instruments(protocol, t, data)


def _instruments(protocol: Protocol, t: list, data: list) -> None:
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
        if not number or raw != _text(padded) + _text(suffix):
            malformed.append(r)
        first = 35 + (r - 2) * 2
        rows = t[first:first + 2]
        values = tuple(tuple(line[c] for c in MEASUREMENT_COLUMNS) for line in rows)
        measured = len(values) == 2 and all(
            type(value) in (int, float) and isfinite(value) for line in values for value in line
        )
        by_index[index].append((number, raw, ElementReading(
            part, _text(status), measured, f"Данные!I{r}, Таблицы!C{first}:J{first + 1}", values if measured else ()
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
        problem = ""
        if len(numbers) != 1 or len({item[1] for item in parts}) != 1:
            problem = "У чувствительных элементов одного прибора расходятся заводские номера."
        elif sorted(item[2].index for item in parts) != list(range(1, protocol.elements + 1)):
            problem = "Недостающий или повторный чувствительный элемент."
        first = 2 + index * protocol.elements
        source = f"Данные!B{first}:I{first + len(parts) - 1} / Таблицы!B19"
        protocol.instruments.append(Instrument(number, parts[0][1], source,
                                              tuple(item[2] for item in parts), problem))
    try:
        batch = serial_list(_text(t[19][1]))
        if protocol.declared and len(batch) != protocol.declared:
            raise ValueError(f"Указано {protocol.declared} СИ, в строке партии найдено {len(batch)} номеров.")
        if batch != [item.serial for item in protocol.instruments]:
            raise ValueError("Список партии и номера в сохранённых строках измерений различаются.")
    except ValueError as exc:
        protocol.serial_issue = str(exc)
        if not protocol.instruments and _present(t[19][1]):
            try:
                protocol.instruments = [Instrument(n, _text(t[19][1]), "Таблицы!B19") for n in serial_list(_text(t[19][1]))]
            except ValueError:
                pass
