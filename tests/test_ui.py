import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QTableView, QTreeWidget

from arshin_generator.arshin import Schema, export
from arshin_generator.storage import Storage
from arshin_generator.ui.dialogs import XmlDialog
from arshin_generator.ui.main import MainWindow
from arshin_generator.ui.style import apply_theme
from tests.test_workflow import SAMPLE, approve, equipment, fixture


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
            main = MainWindow(storage, schema)
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


if __name__ == "__main__":
    unittest.main()
