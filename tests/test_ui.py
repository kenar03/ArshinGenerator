import os
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QItemSelectionModel, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QTableView, QTreeWidget

from arshin_generator.arshin import Schema
from arshin_generator.storage import Storage
from arshin_generator.registry import EquipmentLookup, lookup_equipment, lookup_key, resolve_many
from arshin_generator.ui.dialogs import EquipmentReviewPanel, XmlDialog
from arshin_generator.ui.main import MainWindow
from arshin_generator.ui.style import apply_theme
from arshin_generator.ui.workflow import PreviewDialog, RepairDialog, correction_rows
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
            self.assertEqual(len(main.results), 25)
            self.assertTrue(all(result.value("outcome") == "pass" for result in main.results))
            self.assertFalse(main.export_btn.isEnabled())
            action = main.source.indexWidget(main.source_model.index(0, 3))
            self.assertEqual(action.text(), "Исправить")
            with patch.object(RepairDialog, "exec", return_value=0) as opened:
                QTest.mouseClick(action, Qt.MouseButton.LeftButton)
                opened.assert_called_once()
            protocol = main.protocols[0]
            main.lookups = {
                lookup_key(item, protocol.fields["verification_date"].value):
                EquipmentLookup("unavailable", "Не удалось подключиться к реестру. Повторите проверку")
                for item in protocol.equipment
            }
            main.recheck()
            self.assertEqual(main.source_model.rows[0][1][2], "Нет связи с реестром")
            repair = RepairDialog(main, protocol)
            equipment_panel = repair.equipment_panel
            self.assertEqual(equipment_panel.item_model.rowCount(), 8)
            self.assertNotIn("не найдено", " ".join(row[1][-1] for row in equipment_panel.item_model.rows))
            self.assertFalse({"modification", "sign_pass", "sign_mi"} & repair.party_editor.controls.keys())
            repair.toggle_sign_exceptions(True)
            repair.party_editor.controls["valid_date"].setText("14.01.2027")
            repair.item_editor.set_value("sign_pass", False)
            repair.save()
            self.assertIs(main.results[0].value("sign_pass"), False)
            self.assertTrue(all(result.value("sign_pass") is True for result in main.results[1:]))
            self.assertTrue(all(result.value("valid_date") == "2027-01-14" for result in main.results))
            repair.close()
            preview = PreviewDialog(main, main.results)
            self.assertEqual(preview.model.rowCount(), 25)
            self.assertEqual(preview.model.headers, ("Заводской номер", "Модификация", "Дата поверки", "Результат"))
            self.assertTrue(preview.card_btn.isEnabled())
            with patch("arshin_generator.ui.workflow.QDialog.exec", return_value=0) as opened:
                preview.open_card()
                opened.assert_called_once()
            preview.close()
            storage.save_confirmations(correction_rows(storage.confirmations(), protocol,
                                                       {"sign_pass": True, "sign_mi": False}))
            main.recheck()
            self.assertFalse(main.results[0].value("sign_pass"))
            self.assertTrue(all(result.value("sign_mi") is False for result in main.results))
            p = fixture(path / "ui-test.xlsx", "ТЕСТ-ИНТЕРФЕЙС", "001B6")
            storage.save_equipment(equipment())
            approve(storage, p)
            main.protocols = [p]
            main.lookups.clear()
            main.recheck()
            self.assertTrue(main.export_btn.isEnabled())
            self.assertEqual(main.source_model.rows[0][1][2], "Готов к выгрузке")
            storage.set_setting("output_folder", str(path))
            with patch("arshin_generator.ui.main.QFileDialog.getExistingDirectory") as dialog, \
                    patch.object(main, "show_saved") as saved:
                QTest.mouseClick(main.export_btn, Qt.MouseButton.LeftButton)
                saved.assert_called_once()
                dialog.assert_not_called()
            files, records = saved.call_args.args
            self.assertEqual(len(records), 1)
            self.assertTrue(files[0].is_file())
            context = storage.export_context(storage.history()[0]["id"])
            xml = XmlDialog(main, files, schema, context)
            xml.search.setText("ТЕСТ-ИНТЕРФЕЙС")
            self.assertEqual(xml.model.rowCount(), 1)
            self.assertIn("соответствует", xml.status.text())
            self.assertIn("001B6", xml.raw.toPlainText())
            self.assertIn("01234567890", xml.raw.toPlainText())
            self.assertIn("Тестова А.В.", xml.raw.toPlainText())
            self.assertFalse(xml.tabs.isTabVisible(1))
            xml.toggle_technical(True)
            self.assertTrue(xml.tabs.isTabVisible(1))
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
            dialog = EquipmentReviewPanel(None, storage, protocol, {lookup_key(item, day.isoformat()): found})
            self.assertEqual(dialog.candidate_model.rowCount(), 2)
            self.assertFalse(dialog.select_btn.isEnabled())
            for i in range(2):
                dialog.candidates.selectionModel().select(
                    dialog.candidate_model.index(i, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
                )
            self.assertTrue(dialog.select_btn.isEnabled())
            self.assertEqual(len(dialog.selected_candidates()), 2)
            dialog.choose()
            entries, issue = resolve_many(item, Storage(path / "ui.sqlite3").entries(), day, found)
            self.assertEqual(len(entries), 2)
            self.assertFalse(issue)
            only_one = type(found)("found", "", (found.candidates[0],))
            self.assertFalse(resolve_many(item, storage.entries(), day, only_one)[0])
            dialog.close()


if __name__ == "__main__":
    unittest.main()
