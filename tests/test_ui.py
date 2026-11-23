import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QTableView, QTreeWidget

from arshin_generator.arshin import Schema, export
from arshin_generator.storage import Storage
from arshin_generator.registry import lookup_equipment, lookup_key
from arshin_generator.ui.dialogs import EquipmentReviewDialog, XmlDialog
from arshin_generator.ui.main import MainWindow
from arshin_generator.ui.style import apply_theme
from tests.test_workflow import SAMPLE, approve, equipment, fixture
from tests.test_public_registry import FixtureClient, etalon, mieta


def contrast(foreground, background):
    def luminance(color):
        red, green, blue, _ = color.getRgbF()
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in (red, green, blue)]
        return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))

    lower, upper = sorted([luminance(foreground), luminance(background)])
    return (upper + 0.05) / (lower + 0.05)


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def test_selection_contrast_is_independent_of_system_palette(self):
        role = QPalette.ColorRole
        group = QPalette.ColorGroup
        dark = QPalette()
        dark.setColor(role.Window, QColor("#172239"))
        dark.setColor(role.WindowText, QColor("#080a10"))
        dark.setColor(role.Highlight, QColor("#002754"))
        dark.setColor(role.HighlightedText, QColor("#101419"))
        self.app.setPalette(dark)
        apply_theme(self.app)

        combo = QComboBox()
        combo.addItems(["Первичная", "Периодическая"])
        widgets = [QLineEdit(), QTableView(), QTreeWidget(), combo, combo.view()]
        for widget in widgets:
            widget.ensurePolished()
            palette = widget.palette()
            for state in (group.Active, group.Inactive, group.Disabled):
                with self.subTest(widget=widget.metaObject().className(), state=state):
                    self.assertGreaterEqual(contrast(palette.color(state, role.HighlightedText),
                                                     palette.color(state, role.Highlight)), 4.5)

    def test_import_preview_search_and_xml_viewer(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            storage = Storage(path / "ui.sqlite3")
            schema = Schema()
            main = MainWindow(storage, schema, online=False)
            main.show()
            main.import_paths([SAMPLE])
            if main.thread:
                loop = QEventLoop()
                main.thread.finished.connect(loop.quit)
                QTimer.singleShot(5000, loop.quit)
                loop.exec()
            self.assertIsNone(main.thread)
            self.assertEqual(main.source_model.rowCount(), 1)
            self.assertEqual(main.result_model.rowCount(), 25)
            self.assertFalse(main.export_btn.isEnabled())
            QTest.mouseClick(main.preview_btn, Qt.MouseButton.LeftButton)
            self.assertEqual(main.tabs.currentIndex(), 1)
            main.search.setText("018W6")
            self.assertEqual(main.result_model.rowCount(), 1)
            self.assertIn("018W6", main.card.toPlainText())
            self.assertNotIn("Учебная книга", main.card.toPlainText())
            self.assertNotIn("Нет точного подтверждённого соответствия ФИФ ОЕИ", main.card.toPlainText())
            self.assertTrue(main.protocol_issues_btn.isVisible())
            QTest.mouseClick(main.protocol_issues_btn, Qt.MouseButton.LeftButton)
            self.assertEqual(main.tabs.currentIndex(), 0)
            self.assertEqual(main.card.toPlainText().count("Нет точного подтверждённого соответствия ФИФ ОЕИ"), 8)
            main.show_results()
            self.assertIn("Данные!I", main.card.toPlainText())
            main.search.clear()
            p = fixture(path / "ui-test.xlsx", "ТЕСТ-ИНТЕРФЕЙС", "001B6")
            storage.save_equipment(equipment())
            approve(storage, p)
            main.protocols = [p]
            QTest.mouseClick(main.check_btn, Qt.MouseButton.LeftButton)
            self.assertTrue(main.export_btn.isEnabled())
            self.assertTrue(main.view_record_btn.isEnabled())
            files = export(main.results, schema, path, storage, [], "Тест")
            context = storage.export_context(storage.history()[0]["id"])
            xml = XmlDialog(main, files, schema, context)
            xml.search.setText("ТЕСТ-ИНТЕРФЕЙС")
            self.assertEqual(xml.model.rowCount(), 1)
            self.assertIn("соответствует", xml.status.text())
            self.assertIn("001B6", xml.raw.toPlainText())
            self.assertIn("01234567890", xml.raw.toPlainText())
            self.assertIn("Тестова А.В.", xml.raw.toPlainText())
            xml.close()
            main.close()

    def test_equipment_candidates_require_an_explicit_selection(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            storage = Storage(path / "ui.sqlite3")
            protocol = fixture(path / "ui.xlsx", "ТЕСТ-КАРТОЧКИ", "001B6")
            item = etalon()
            protocol.equipment = [item]
            day = date.fromisoformat(protocol.fields["verification_date"].value)
            first = ("41", "32777.06.2Р.01000001", "ГПС 1")
            second = ("42", "32777.06.2Р.01000002", "ГПС 2")
            cases = []
            for args in (first, second):
                row, detail = mieta(*args)
                detail["cresults"][0]["verification_date"] = "01.01.2026"
                cases.append((row, detail))
            found = lookup_equipment(item, day, FixtureClient(cases))
            self.assertEqual(found.state, "choice")
            dialog = EquipmentReviewDialog(None, storage, protocol, {lookup_key(item, day.isoformat()): found})
            self.assertEqual(dialog.candidate_model.rowCount(), 2)
            self.assertFalse(dialog.select_btn.isEnabled())
            dialog.candidates.selectRow(1)
            self.assertTrue(dialog.select_btn.isEnabled())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
