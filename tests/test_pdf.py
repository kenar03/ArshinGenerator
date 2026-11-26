from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter

from arshin_generator.domain import PdfError
from arshin_generator.excel_pdf import ExcelSession, render_workbook
from arshin_generator.pdf_export import ExcelInterrupted, ExcelUnavailable, create_pdfs, plan_pdf, verify_pdf
from arshin_generator.readers.rtd_print import prepare_pdf
from tests.test_workflow import fixture


def write_pdf(path, *, pages=1):
    with PdfWriter() as writer:
        for _ in range(pages):
            writer.add_blank_page(595, 842)
        writer.write(path)


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "PDF"
        self.destination.mkdir()

    def book(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fixture(path, "Проверка", "001B6", temperature=None, owner="")
        return path

    def session(self):
        session = Mock()
        session.render.side_effect = lambda _, source, temporary: write_pdf(temporary)
        return session

    def test_batch_same_names_hashes_progress_and_error_continuation(self):
        first = self.book("П-Т1-з2-0260-26.xlsm")
        broken = self.root / "Битая.xlsx"
        broken.write_bytes(b"not a workbook")
        last = self.book("П-Т1-з2-0261-26.xlsx")
        paths = (first, broken, last)
        digests = [sha256(path.read_bytes()).digest() for path in paths]
        session = self.session()
        progress = Mock()
        results = create_pdfs(plan_pdf(paths, self.destination), session_factory=lambda: session, progress=progress)
        self.assertEqual([item.status for item in results], ["created", "error", "created"])
        self.assertEqual([call.args for call in progress.call_args_list], [(p, i, 3) for i, p in enumerate(paths, 1)])
        self.assertEqual({p.name for p in self.destination.iterdir()}, {first.stem + ".pdf", last.stem + ".pdf"})
        self.assertEqual([sha256(path.read_bytes()).digest() for path in paths], digests)
        self.assertEqual([call.args[1] for call in session.render.call_args_list], [first, last])
        session.close.assert_called_once()

    def test_existing_skip_duplicate_and_safe_replacement(self):
        files = [self.book("a/Повтор.xlsx"), self.book("b/Повтор.xlsm"), self.book("Другой.xlsx")]
        existing = self.destination / "Повтор.pdf"
        existing.write_bytes(b"previous report")
        session = self.session()
        jobs = plan_pdf(files, self.destination)
        results = create_pdfs(jobs, session_factory=lambda: session)
        self.assertEqual([item.status for item in results], ["skipped", "skipped", "created"])
        self.assertEqual(existing.read_bytes(), b"previous report")

        def malformed(_, __, target):
            target.write_bytes(b"invalid PDF")

        session.render.side_effect = malformed
        results = create_pdfs(jobs[:2], replace_existing=True, session_factory=lambda: session)
        self.assertEqual([item.status for item in results], ["error", "skipped"])
        self.assertEqual(existing.read_bytes(), b"previous report")

        results = create_pdfs(jobs[:2], replace_existing=True, session_factory=self.session)
        self.assertEqual([item.status for item in results], ["created", "skipped"])
        verify_pdf(existing)

    def test_lost_excel_restarts_and_does_not_stop_following_book(self):
        failed, working = self.book("a.xlsx"), self.book("b.xlsx")
        old, fresh = self.session(), self.session()
        old.render.side_effect = ExcelInterrupted("Excel завис")
        factory = Mock(side_effect=(old, fresh))
        results = create_pdfs(plan_pdf([failed, working], self.destination), session_factory=factory)
        self.assertEqual([item.status for item in results], ["error", "created"])
        old.close.assert_called_once()
        fresh.close.assert_called_once()

    def test_password_unsupported_module_and_unavailable_excel(self):
        plain = self.book("a.xlsx")
        password = self.root / "secret.xlsx"
        password.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1"))
        module = SimpleNamespace(VERSION="different", DISPLAY_NAME="Другой", matches=lambda _: True)
        factory = Mock(side_effect=ExcelUnavailable("Нет Excel"))
        with patch("arshin_generator.readers.READERS", (module,)):
            results = create_pdfs(plan_pdf([password, plain], self.destination), session_factory=factory)
        self.assertIn("паролем", results[0].detail)
        self.assertIn("не поддерживает PDF", results[1].detail)
        factory.assert_not_called()
        self.assertEqual(create_pdfs(plan_pdf([plain], self.destination), session_factory=factory)[0].detail, "Нет Excel")

    def test_pdf_validation_rejects_empty_or_missing_pages(self):
        invalid = self.root / "invalid.pdf"
        invalid.write_bytes(b"")
        with self.assertRaises(PdfError):
            verify_pdf(invalid)
        write_pdf(invalid, pages=0)
        with self.assertRaises(PdfError):
            verify_pdf(invalid)
        write_pdf(invalid, pages=2)
        verify_pdf(invalid)


class ComContractTests(unittest.TestCase):
    def test_workbook_opens_readonly_and_uses_only_adapter_sheets(self):
        app, book, report = Mock(), Mock(), Mock()
        source = Path("protocol.xlsm").absolute()
        output = source.with_suffix(".pdf")
        book.ReadOnly = True
        book.FullName = str(source)
        book.Worksheets.return_value = report
        app.Workbooks.Open.return_value = book
        adapter = Mock(return_value=("Отчёт",))
        sentinel = object()

        render_workbook(app, adapter, source, output, sentinel)
        app.Workbooks.Open.assert_called_once_with(str(source), 0, True, sentinel, "", "", True,
                                                 sentinel, sentinel, True, False, sentinel, False, False, 0)
        app.CalculateFull.assert_called_once()
        adapter.assert_called_once_with(book, source)
        book.Worksheets.assert_called_once_with("Отчёт")
        report.ExportAsFixedFormat.assert_called_once_with(0, str(output), 0, True, False, sentinel, sentinel, False)
        book.Close.assert_called_once_with(False)

        book.Close.reset_mock()
        report.ExportAsFixedFormat.side_effect = RuntimeError("write")
        with self.assertRaisesRegex(RuntimeError, "write"):
            render_workbook(app, adapter, source, output, sentinel)
        book.Close.assert_called_once_with(False)

        book.ReadOnly = False
        with self.assertRaisesRegex(PdfError, "в режиме чтения"):
            render_workbook(app, adapter, source, output, sentinel)

    def test_timeout_triggers_forced_cleanup(self):
        session = ExcelSession.__new__(ExcelSession)
        session.connection = Mock()
        session.connection.poll.return_value = False
        session.close = Mock()
        with self.assertRaisesRegex(ExcelInterrupted, "не ответил"):
            session.render("rtd-v1", Path("input.xlsm"), Path("output.pdf"))
        session.close.assert_called_once_with(force=True)


class PrintAdapterTests(unittest.TestCase):
    def workbook(self, count, scheme, modern, flags):
        book, report, tables, catalog = Mock(), Mock(), Mock(), Mock()
        book.Worksheets.side_effect = {"Отчёт": report, "Таблицы": tables, "Оборудование": catalog}.__getitem__
        header = 353 if modern else 62
        appendix = 345 if modern else 54
        tables.Range.side_effect = lambda a: SimpleNamespace(Value2={"D1": "Протокол", "J27": count, "H27": scheme}[a])
        catalog.Range.side_effect = lambda a: SimpleNamespace(Value2={"F1": "Используется" if modern else "Примечание",
                                                                    "F2:F301": tuple((v,) for v in flags)}[a])
        report.PageSetup.PrintArea = "$A$1:$Y$2364" if modern else "$A$1:$Y$2073"
        report.PageSetup.PaperSize = 1
        original_area = report.PageSetup.PrintArea
        print_range = SimpleNamespace(Areas=SimpleNamespace(Count=1), Row=1, Column=1,
                                      Rows=SimpleNamespace(Count=header + 2011), Columns=SimpleNamespace(Count=25))
        report.Cells.side_effect = lambda r, c: (r, c)
        values = {"A20": "Протокол поверки № Протокол", f"A{appendix}": "Приложение № Протокол",
                  f"A{header}": "№", f"D{header}": "Rэт, Ом", "A35:A334": (("Средство",),) * 300}

        def range_(start, end=None):
            if start == original_area:
                return print_range
            if end is None:
                return SimpleNamespace(Value2=values[start])
            return SimpleNamespace(Value2=((None,),), Address=f"$A$1:$Y${end[0]}")

        report.Range.side_effect = range_
        rows = {}
        report.Rows.side_effect = lambda key: rows.setdefault(key, SimpleNamespace(Hidden=None))
        return book, report, rows

    def test_layout_uses_only_reserved_rows_and_a4(self):
        for count, scheme, modern in ((1.0, 4.0, True), (20, "2х4", True), (100, "3", False)):
            with self.subTest(count=count, modern=modern):
                book, report, rows = self.workbook(count, scheme, modern, ["+", "", "+"])
                self.assertEqual(prepare_pdf(book, Path("Протокол.xlsm")), ("Отчёт",))
                top = 353 if modern else 62
                last = top + int(count) * (4 if scheme == "2х4" else 2)
                self.assertFalse(rows[f"{top + 1}:{last}"].Hidden)
                self.assertTrue(rows[f"{last + 1}:{top + 2000}"].Hidden)
                self.assertEqual(report.PageSetup.PrintArea, f"$A$1:$Y${last}")
                self.assertEqual(report.PageSetup.PaperSize, 9)
                if modern:
                    self.assertFalse(rows["35:36"].Hidden)
                    self.assertTrue(rows["37:334"].Hidden)
                self.assertNotIn("1:34", rows)

    def test_wrong_protocol_number_is_blocked(self):
        book, _, rows = self.workbook(1, 4, True, ["+"])
        with self.assertRaisesRegex(PdfError, "номер протокола"):
            prepare_pdf(book, Path("Переименован.xlsm"))
        self.assertFalse(rows)
