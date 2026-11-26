from ..domain import PdfError


def _text(cell):
    value = cell.Value2
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() if value is not None else ""


def _contains_number(sheet, cell, expected):
    return _text(sheet.Range(cell)).partition("№")[2].strip() == expected


def _visible_rows(report, first, last, visible):
    if visible:
        report.Rows(f"{first}:{first + visible - 1}").Hidden = False
    if first + visible <= last:
        report.Rows(f"{first + visible}:{last}").Hidden = True


def prepare_pdf(book, source):
    """Prepare only the sections handled by the supplied Workbook_BeforePrint code."""
    report, tables = book.Worksheets("Отчёт"), book.Worksheets("Таблицы")
    catalog = book.Worksheets("Оборудование")
    modern = _text(catalog.Range("F1")).casefold() == "используется"
    appendix, header = (345, 353) if modern else (54, 62)
    number = _text(tables.Range("D1"))
    if (number != source.stem or "!!!!!" in number or not _contains_number(report, "A20", number)
            or not _contains_number(report, f"A{appendix}", number)):
        raise PdfError("После пересчёта номер протокола не совпадает с именем книги. Проверьте Таблицы!D1 и заголовки отчёта.")
    if _text(report.Range(f"A{header}")) != "№" or not _text(report.Range(f"D{header}")).casefold().startswith("rэт"):
        raise PdfError("Не найдена таблица измерений в приложении на листе «Отчёт».")

    devices = tables.Range("J27").Value2
    if type(devices) not in (int, float) or not 1 <= devices <= 500 or devices != int(devices):
        raise PdfError("Укажите количество приборов от 1 до 500 в Таблицы!J27.")
    scheme = _text(tables.Range("H27")).casefold().replace("x", "х").replace("×", "х")
    if scheme not in {"2", "3", "4", "2х2", "2х3", "2х4"}:
        raise PdfError("Проверьте схему соединения в Таблицы!H27.")
    rows = 2 * int(devices) * (2 if "х" in scheme else 1)

    setup = report.PageSetup
    area = setup.PrintArea
    if area:
        print_range = report.Range(area)
    else:
        # Older books can expose the saved area only as a sheet-local defined name.
        print_range = next((name.RefersToRange for name in report.Names
                            if name.Name.rsplit("!", 1)[-1].casefold() in {"print_area", "_xlnm.print_area"}), None)
    if print_range is None:
        raise PdfError("На листе «Отчёт» не задана область печати.")
    last_reserved = header + 2000
    if (print_range.Areas.Count != 1 or print_range.Row != 1
            or print_range.Rows.Count < header + rows):
        raise PdfError("Область печати листа «Отчёт» не включает все строки протокола.")

    if modern:
        selected = sum(isinstance(flag, str) and flag == "+" for (flag,) in catalog.Range("F2:F301").Value2)
        labels = report.Range("A35:A334").Value2
        if any(not isinstance(value, str) or not value.strip() for (value,) in labels[:selected]):
            raise PdfError("Выбранные средства поверки не отображаются в отчёте. Проверьте Оборудование!A2:F301.")
        _visible_rows(report, 35, 334, selected)

    _visible_rows(report, header + 1, last_reserved, rows)
    # Keep authored content below the reserved rows, and trim an unused trailing print margin.
    last = print_range.Rows.Count
    left, right = print_range.Column, print_range.Column + print_range.Columns.Count - 1
    if last > last_reserved:
        tail = report.Range(report.Cells(last_reserved + 1, left), report.Cells(last, right)).Value2
        if not any(value not in (None, "") for row in tail for value in row):
            last = header + rows
    setup.PrintArea = report.Range(report.Cells(1, left), report.Cells(last, right)).Address
    if setup.PaperSize != 9:  # xlPaperA4
        setup.PaperSize = 9
    return ("Отчёт",)
