import json
import os
from dataclasses import replace
from datetime import date
from errno import ENOSPC
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from openpyxl import load_workbook
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QTableView,
    QTreeWidget,
)

from arshin_generator.arshin import NS, SCHEMA_HASH, Schema, export, format_packets, open_xml, result_element, signature
from arshin_generator.confirmations import protocol_key
from arshin_generator.domain import Datum, RegistryEntry
from arshin_generator.readers import read_protocol
from arshin_generator.storage import Storage
from arshin_generator.registry import EquipmentCandidate, EquipmentLookup, lookup_equipment, lookup_key
from arshin_generator.ui.common import Form
from arshin_generator.ui.dialogs import HistoryDialog, XmlDialog
from arshin_generator.ui.main import MainWindow
from arshin_generator.ui.style import apply_theme
from arshin_generator.ui.protocols import ProtocolDialog, serial_summary
from arshin_generator.verification import RegistrySession
from tests.test_workflow import INCOMPLETE, SAMPLE, SAMPLE_METHOD, equipment, fixture, sample_copy_with_kind, seed_lab
from tests.test_public_registry import FixtureClient, etalon, mieta, registry_http


def contrast(foreground, background):
    def luminance(color):
        red, green, blue, _ = color.getRgbF()
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in (red, green, blue)]
        return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))

    lower, upper = sorted([luminance(foreground), luminance(background)])
    return (upper + 0.05) / (lower + 0.05)


def preview_fields(dialog):
    rows = {}
    for form in dialog.scroll.widget().findChildren(QFormLayout):
        for index in range(form.rowCount()):
            caption = form.itemAt(index, QFormLayout.ItemRole.LabelRole).widget()
            field = form.itemAt(index, QFormLayout.ItemRole.FieldRole).widget()
            rows[caption.text()] = field.text()
    return rows


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


    def window(self, folder, *, online=False, registry_session=None):
        main = MainWindow(Storage(folder / "ui.sqlite3"), Schema(), online=online, registry_session=registry_session)
        self.addCleanup(main.close)
        return main

    def wait_for_import(self, main):
        for attribute in ("thread", "lookup_thread"):
            thread = getattr(main, attribute)
            if not thread:
                continue
            loop = QEventLoop()
            thread.finished.connect(loop.quit)
            QTimer.singleShot(5000, loop.quit)
            loop.exec()
            self.assertIsNone(getattr(main, attribute))

    def verify_protocols(self, main):
        main.type_titles = {"56560-14": "Термопреобразователи сопротивления"}
        main.lookups = {
            lookup_key(item, p.fields["verification_date"].value): EquipmentLookup("found", "", (
                EquipmentCandidate(RegistryEntry(
                    item.description, item.source_id, item.serial, item.name, item.category, item.registration,
                    item.type_number, item.serial, "manufactureNum", "2025-01-01", "2027-01-14",
                    "Ответ реестра для теста", "Публичный ФИФ ОЕИ", public_id=f"карточка-{item.serial}",
                    automatic=True, version_hash=f"версия-{item.serial}",
                ), "", True),
            ))
            for p in main.protocols for item in p.equipment
        }
        main.recheck()

    def test_control_protocol_repeat_export_without_warning(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            file = path / "Мой XML.xml"
            main.import_paths([sample_copy_with_kind(path / "Изолированное испытание.xlsm", "периодической")])
            self.wait_for_import(main)
            [protocol] = main.protocols
            main.storage.save_confirmation("protocol", protocol_key(protocol), {
                "verification_type": "2", "sign_cipher": "ТСТ", "calibration": False, "valid_date": "2027-01-14",
            }, "Сохранившиеся реквизиты", "Тест")
            self.assertEqual(main.source_model.headers, ("", "Протокол", "Тип протокола", "Приборов", "Состояние"))
            self.assertEqual(main.source.verticalHeader().defaultSectionSize(), 36)
            self.verify_protocols(main)
            self.assertTrue(main.export_btn.isEnabled())
            self.assertEqual(main.source_model.rows[0][1][2:], ("ТСП/ТСМ", 25, "Готов"))
            self.assertFalse(main.notice.text())
            overview = ProtocolDialog(main, protocol)
            self.assertEqual(overview.findChildren(QLineEdit), [overview.year_input])
            self.assertEqual(overview.year_input.text(), "")
            texts = "\n".join(label.text() for label in overview.findChildren(QLabel))
            self.assertIn("56560-14 · Термопреобразователи сопротивления", texts)
            self.assertIn("Тип СИ в реестре ФИФ ОЕИ", texts)
            self.assertIn("ТСМ-0193-01", texts)
            self.assertIn("Модификация СИ", texts)
            self.assertNotIn("Модель и модификация", texts)
            self.assertIn("Готов к выгрузке", texts)
            self.assertIn("Владелец СИ", texts)
            self.assertIn("ФИО поверителя", texts)
            self.assertIn("Документ на методику поверки", texts)
            self.assertIn("22.11.2026", texts)
            self.assertIn("Приборов: 25 · Пригодны: 25 · Непригодны: 0", texts)
            self.assertIn("009W6–033W6 (25 приборов)", texts)
            self.assertNotIn("Причины непригодности", texts)
            self.assertIn("Средства поверки: проверено 7 из 7", texts)
            self.assertEqual(set(overview.equipment_models), {"mieta", "mis"})
            self.assertEqual(overview.instrument_model.rowCount(), 3)
            overview.serial_toggle.click()
            self.assertEqual(overview.serial_toggle.text(), "Свернуть")
            preview_serials = [row[1][0] for row in overview.instrument_model.rows]
            overview.serial_toggle.click()
            self.assertEqual(overview.instrument_model.rowCount(), 3)
            reviewed_equipment = [row[1][2] for model in overview.equipment_models.values() for row in model.rows]
            self.assertIn("32777.06.2Р.01504903", reviewed_equipment)
            overview.close()

            with patch.object(main, "choose_xml_path", return_value=file) as dialog, \
                    patch("arshin_generator.ui.main.message") as prompt:
                main.export_btn.click()
                self.assertEqual(dialog.call_args.args[0].name, "СИ-Экспорт_П-Т1-з2-1260-26.xml")
                prompt.assert_not_called()
            self.assertEqual(main.notice.text(), f"XML сохранён: 25 записей\n{file}")
            root, issues, _ = open_xml(file, main.schema)
            self.assertFalse(issues)
            self.assertEqual([signature(r) for r in root], [signature(result_element(r)) for r in main.results])
            self.assertFalse(root.findall(f".//{{{NS}}}stickerNum"))
            self.assertEqual(root.findtext(f".//{{{NS}}}docTitle"), SAMPLE_METHOD)
            self.assertEqual(root.xpath("g:result/g:miInfo/g:singleMI/g:manufactureNum/text()", namespaces={"g": NS}),
                             preview_serials)
            exported_equipment = root[0].xpath("g:means/g:mieta/g:number/text() | g:means/g:mis/g:mi/g:typeNum/text()",
                                               namespaces={"g": NS})
            self.assertCountEqual(exported_equipment, reviewed_equipment)
            self.assertEqual([r.instrument.serial for r in main.results], [f"{i:03d}W6" for i in range(9, 34)])
            for record in main.results:
                self.assertEqual(len(record.equipment), 7)
                self.assertIs(record.value("sign_pass"), True)
                self.assertIs(record.value("sign_mi"), False)
                self.assertEqual(record.value("valid_date"), "2030-11-21")
            [history] = main.storage.history()
            self.assertEqual(history["schema_hash"], SCHEMA_HASH)
            self.assertEqual(history["directory"], str(path))
            self.assertEqual(json.loads(history["files"]), [file.name])
            viewer = XmlDialog(main, [file], main.schema, main.storage.export_context(history["id"]))
            viewer.search.setText("009W6")
            self.assertEqual(viewer.model.rowCount(), 1)
            self.assertIn("соответствует", viewer.status.text())
            viewer.close()
            repeated = path / "Повтор.xml"
            with patch.object(main, "choose_xml_path", return_value=repeated) as dialog, \
                    patch("arshin_generator.ui.main.message") as prompt:
                main.export_btn.click()
                prompt.assert_not_called()
                dialog.assert_called_once()
            self.assertEqual(repeated.read_bytes(), file.read_bytes())
            self.assertEqual(len(main.storage.history()), 2)
            main.close()

    def test_open_excel_uses_current_original_without_rechecking(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            first = fixture(path / "Первый файл.xlsx", "ТЕСТ-1", "001B6")
            second = fixture(path / "Второй файл.xlsm", "ТЕСТ-2", "002B6")
            main.protocols = [first, second]
            main.recheck()
            dialog = ProtocolDialog(main, first)
            before = {p.path: p.path.read_bytes() for p in (first, second)}
            with patch("arshin_generator.ui.protocols.QDesktopServices.openUrl", return_value=True) as launch, \
                    patch("arshin_generator.ui.protocols.message") as error, \
                    patch.object(main, "recheck") as recheck, \
                    patch.object(main, "start_registry_checks") as check:
                dialog.open_excel_btn.click()
                dialog.selector.setCurrentIndex(1)
                dialog.open_excel_btn.click()
                self.assertEqual([Path(call.args[0].toLocalFile()) for call in launch.call_args_list],
                                 [first.path, second.path])
                error.assert_not_called()
                check.assert_not_called()
                recheck.assert_not_called()
            self.assertEqual({p: p.read_bytes() for p in before}, before)
            dialog.close()

    def test_open_excel_reports_missing_file_and_failed_association(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            protocol = fixture(path / "Файл.xltm", "ТЕСТ", "001B6")
            main.protocols = [protocol]
            main.recheck()
            dialog = ProtocolDialog(main)
            with patch("arshin_generator.ui.protocols.QDesktopServices.openUrl", return_value=False) as launch, \
                    patch("arshin_generator.ui.protocols.message") as error:
                dialog.open_excel_btn.click()
                launch.assert_called_once()
                error.assert_called_once()
                protocol.path.rename(path / "Перемещённый.xltm")
                launch.reset_mock()
                error.reset_mock()
                dialog.open_excel_btn.click()
                launch.assert_not_called()
                self.assertEqual(error.call_args.args[2],
                                 "Не удалось открыть исходный протокол. Проверьте, существует ли файл.")
                error.assert_called_once()
            dialog.close()

    def test_preview_identifies_only_failed_instruments_in_a_separate_section(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            file = path / "mixed.xlsx"
            fixture(file, "ТЕСТ-СМЕШАННЫЙ", "unused", count=2, sticker="")
            workbook = load_workbook(file)
            workbook["Таблицы"]["C19"] = "не годны к применению"
            workbook["Таблицы"]["F20"] = "Отклонение за пределами допуска"
            workbook["Таблицы"]["H37"] = 101
            workbook["Данные"]["I3"] = "НЕ СООТВЕТСТВУЕТ"
            workbook["Отчёт"]["A45"] = "Заключение: СИ не годны к применению."
            workbook.save(file)
            workbook.close()
            protocol = read_protocol(file)
            main = self.window(path)
            seed_lab(main.storage)
            main.protocols = [protocol]
            self.verify_protocols(main)
            self.assertTrue(main.can_export)
            dialog = ProtocolDialog(main, protocol)
            texts = [label.text() for label in dialog.findChildren(QLabel)]
            self.assertIn("Приборов: 2 · Пригодны: 1 · Непригодны: 1", texts)
            self.assertEqual([row[1] for row in dialog.instrument_model.rows],
                             [("001B6", "2026", "Пригодно"), ("002B6", "2026", "Непригодно")])
            self.assertEqual(preview_fields(dialog)["Причины непригодности"], "Отклонение за пределами допуска")
            self.assertIsNone(dialog.serial_toggle)
            payload, _ = format_packets(main.results, main.schema)[0]
            root, issues, _ = main.schema.inspect(payload)
            self.assertFalse(issues)
            self.assertEqual(root.xpath("g:result[g:inapplicable]/g:miInfo/g:singleMI/g:manufactureNum/text()",
                                        namespaces={"g": NS}), ["002B6"])
            self.assertIsNone(root[1].find(f"{{{NS}}}validDate"))
            dialog.close()

    def test_large_batch_keeps_exact_numbers_in_one_scroll_area(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            protocol = fixture(path / "large.xlsx", "ТЕСТ-ПАРТИЯ", "unused", count=500, sticker="")
            irregular = (("003412", "003412"), ("005412", "005412"), ("1548-3-1", "1548-3-1"), ("900В6", "900B6"))
            for index, (original, normalized) in enumerate(irregular):
                protocol.instruments[index] = replace(protocol.instruments[index], raw=original, serial=normalized)
            main = self.window(path)
            seed_lab(main.storage)
            main.protocols = [protocol]
            self.verify_protocols(main)
            dialog = ProtocolDialog(main, protocol)
            dialog.resize(510, 420)
            dialog.show()
            self.app.processEvents()
            labels = dialog.findChildren(QLabel)
            self.assertEqual(dialog.instrument_model.rowCount(), 3)
            self.assertIn("003412, 005412, 1548-3-1 и ещё 497", [label.text() for label in labels])
            dialog.serial_toggle.click()
            self.app.processEvents()
            self.assertEqual(len(dialog.findChildren(QLabel)), len(labels))
            numbers = [row[1][0] for row in dialog.instrument_model.rows]
            self.assertEqual(len(numbers), 500)
            self.assertEqual(numbers[3], "900B6")
            self.assertEqual(numbers[-1], "500B6")
            self.assertGreater(dialog.scroll.verticalScrollBar().maximum(), 0)
            self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)
            self.assertTrue(all(view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                                for view in dialog.findChildren(QTableView)))
            dialog.close()

    def test_range_requires_uninterrupted_sequence_and_exact_format(self):
        for numbers in (
            ["009W6", "010W6", "012W6", "013W6"],
            ["001", "002", "003", "0004"],
            ["9W6", "10W6", "11W6", "12W6"],
            ["1A6", "2A6", "3A7", "4A6"],
            ["001-1-2", "002-1-2", "003-1-2", "004-1-2"],
            ["013W6", "012W6", "011W6", "010W6"],
        ):
            with self.subTest(numbers=numbers):
                self.assertEqual(serial_summary(numbers), ", ".join(numbers[:3]) + " и ещё 1")
        self.assertEqual(serial_summary(["009W6", "010W6", "011W6", "012W6"]), "009W6–012W6 (4 прибора)")

    def test_missing_fields_prevent_registry_search_and_corrected_book_replaces_row(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            file = path / "Проверка.xlsx"
            fixture(file, "ТЕСТ-ПОВТОР", "001B6", complete=False, sticker="", owner="", method="")
            main = self.window(path, online=True)
            with patch("arshin_generator.ui.main.RegistryCheckThread") as search:
                main.import_paths([file])
                self.wait_for_import(main)
                search.assert_not_called()
            self.assertFalse(main.export_btn.isEnabled())
            [record] = main.results
            self.assertTrue(record.incomplete_measurements)
            common = " ".join(record.shared_issues)
            self.assertIn("владельца", common)
            self.assertIn("Не указана методика поверки", common)
            self.assertIn("лист «Отчёт», ячейку A33", common)
            overview = ProtocolDialog(main, main.protocols[0])
            self.assertEqual(overview.findChildren(QLineEdit), [overview.year_input])
            overview.close()

            updated = fixture(file, "ТЕСТ-ПОВТОР", "001B6", sticker="")
            self.assertNotEqual(updated.digest, record.protocol.digest)
            seed_lab(main.storage)
            main.online = False
            main.import_paths([file])
            self.wait_for_import(main)
            self.assertEqual([p.digest for p in main.protocols], [updated.digest])
            self.assertEqual(len(main.registry_protocols), 1)
            self.assertFalse(main.export_btn.isEnabled())  # MIT and equipment still need a live response.
            main.close()

    def test_all_or_nothing_and_removal_using_button_and_delete(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            first = fixture(path / "a.xlsx", "ТЕСТ-1", "001B6", sticker="")
            second = fixture(path / "b.xlsx", "ТЕСТ-2", "002B6", sticker="", method="")
            third = fixture(path / "c.xlsx", "ТЕСТ-3", "003B6", sticker="")
            main = self.window(path)
            for protocol in (first, second, third):
                seed_lab(main.storage)
            main.show()
            main.import_paths([first.path, second.path, third.path])
            self.wait_for_import(main)
            self.verify_protocols(main)
            self.assertEqual(len(main.protocols), 3)
            self.assertFalse(main.remove_btn.isEnabled())
            self.assertEqual(sum(not item.blockers for item in main.results), 2)
            self.assertFalse(main.export_btn.isEnabled())
            with patch.object(main, "choose_xml_path") as dialog:
                main.export_xml()
                dialog.assert_not_called()
            main.source.selectRow(1)
            self.assertFalse(main.remove_btn.isEnabled())
            state = Qt.CheckState
            model = main.source_model
            header = main.source.horizontalHeader().select_all
            main.source.setCurrentIndex(model.index(1, 0))
            self.app.processEvents()
            rect = main.source.visualRect(model.index(1, 0))
            QTest.mouseClick(main.source.viewport(), Qt.MouseButton.LeftButton,
                             pos=rect.center())
            self.assertEqual(model.marked_paths, {second.path})
            self.assertEqual(header.checkState(), state.PartiallyChecked)
            self.assertTrue(main.remove_btn.isEnabled())
            main.source.selectRow(0)
            main.source.setFocus()
            QTest.keyClick(main.source, Qt.Key.Key_Delete)
            self.assertEqual([p.path for p in main.protocols], [first.path, third.path])
            self.assertEqual(header.checkState(), state.Unchecked)
            self.assertTrue(main.export_btn.isEnabled())
            model.setData(model.index(0, 0), state.Checked, Qt.ItemDataRole.CheckStateRole)
            with patch.object(main, "choose_xml_path", return_value=path / "all.xml"):
                main.export_btn.click()
            root, issues, _ = open_xml(path / "all.xml", main.schema)
            self.assertFalse(issues)
            self.assertEqual(root.xpath("g:result/g:miInfo/g:singleMI/g:manufactureNum/text()", namespaces={"g": NS}),
                             ["001B6", "003B6"])
            QTest.mouseClick(header, Qt.MouseButton.LeftButton)
            self.assertEqual(header.checkState(), state.Checked)
            QTest.mouseClick(header, Qt.MouseButton.LeftButton)
            self.assertEqual(header.checkState(), state.Unchecked)
            self.assertFalse(main.remove_btn.isEnabled())
            self.assertTrue(main.export_btn.isEnabled())
            QTest.mouseClick(header, Qt.MouseButton.LeftButton)
            main.remove_btn.click()
            self.assertIn("Протоколов 0", main.summary.text())
            self.assertFalse(main.remove_btn.isEnabled())
            self.assertFalse(main.export_btn.isEnabled())
            self.assertTrue(all(protocol.path.is_file() for protocol in (first, second, third)))
            main.close()

    def test_offline_and_unknown_mit_block_despite_saved_equipment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            p = fixture(path / "one.xlsx", "ТЕСТ-ТИП", "001B6", sticker="")
            main = self.window(path)
            seed_lab(main.storage)
            main.storage.save_equipment(equipment())
            main.protocols = [p]
            self.verify_protocols(main)
            self.assertTrue(main.can_export)
            main.type_titles.clear()
            main.type_issues["56560-14"] = "В реестре не найден точный тип СИ 56560-14."
            main.recheck()
            self.assertEqual(main.source_model.rows[0][1][-1], "Требуется исправление")
            type_dialog = ProtocolDialog(main, p)
            self.assertIn("не найден точный тип СИ", "\n".join(label.text() for label in type_dialog.findChildren(QLabel)))
            type_dialog.close()
            self.assertFalse(main.can_export)
            item = p.equipment[0]
            key = lookup_key(item, p.fields["verification_date"].value)
            main.type_titles["56560-14"] = "Термопреобразователи сопротивления"
            main.lookups[key] = EquipmentLookup("unavailable", "Не удалось подключиться к реестру")
            main.recheck()
            self.assertEqual(main.source_model.rows[0][1][-1], "Не удалось проверить")
            self.assertFalse(main.results[0].data_issues)
            self.assertFalse(main.can_export)
            dialog = ProtocolDialog(main, p)
            texts = "\n".join(label.text() for label in dialog.findChildren(QLabel))
            self.assertIn("Средства поверки: проверено 0 из 1", texts)
            self.assertIn("Проверьте связь с ФИФ ОЕИ и нажмите «Перепроверить»", texts)
            self.assertNotIn("Что нужно исправить", texts)
            dialog.close()
            main.close()

    def test_incomplete_real_workbook_groups_measurements_and_short_settings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path, online=True)
            seed_lab(main.storage)
            with patch("arshin_generator.ui.main.RegistryCheckThread") as search:
                main.import_paths([INCOMPLETE])
                self.wait_for_import(main)
                search.assert_not_called()
            self.assertFalse(main.can_export)
            self.assertIsNone(main.settings_btn.menu())
            dialog = ProtocolDialog(main, main.protocols[0])
            detail = "\n".join(label.text() for label in dialog.findChildren(QLabel))
            self.assertEqual(detail.count("Приборов с незавершёнными измерениями: 25"), 1)
            self.assertIn("Приборов: 25 · Пригодны: 0 · Непригодны: 0 · Проблемных: 25", detail)
            self.assertIn("Что нужно исправить", detail)
            self.assertNotIn("Готов к выгрузке", detail)
            self.assertNotIn("Средства поверки: проверено", detail)
            self.assertIn("и ещё 21", detail)
            self.assertEqual(dialog.findChildren(QLineEdit), [dialog.year_input])
            dialog.close()
            main.close()

    def test_other_registered_type_is_verified_separately_and_export_stays_blocked(self):
        def customize(book):
            book["Таблицы"]["L27"] = "34039-07"

        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            main = self.window(path, online=True, registry_session=RegistrySession(opener=server.opener))
            seed_lab(main.storage)
            p = fixture(path / "other.xlsx", "ТЕСТ-ТИП", "009B6", model="ТСП-9204", sticker="", customize=customize)
            main.import_paths([p.path])
            self.wait_for_import(main)
            [url] = server.calls
            self.assertIn("/mit?number=34039-07", url)
            self.assertEqual(sum(server.calls.values()), 1)
            self.assertEqual(main.type_titles, {"34039-07": "Тип 34039-07"})
            self.assertFalse(main.export_btn.isEnabled())
            preview = ProtocolDialog(main)
            texts = "\n".join(label.text() for label in preview.findChildren(QLabel))
            self.assertIn("34039-07 · Тип 34039-07", texts)
            self.assertEqual(texts.count("нет подтверждённого правила"), 1)
            self.assertNotIn("не найден", texts)
            self.assertNotIn("имеет неверный формат", texts)
            preview.close()

    def test_preview_groups_instrument_errors_with_each_location_and_separates_notices(self):
        def customize(book):
            table = book["Таблицы"]
            table["H35"] = table["H37"] = 101

        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            p = fixture(path / "rows.xlsx", "ТЕСТ-ГРУППА", "", count=2, print_limit=1, customize=customize)
            seed_lab(main.storage)
            main.protocols = [p]
            main.recheck()
            preview = ProtocolDialog(main)
            texts = "\n".join(label.text() for label in preview.findChildren(QLabel))
            self.assertEqual(texts.count("Сохранённый результат ЧЭ расходится с допусками"), 1)
            self.assertIn("Для № 001B6 проверьте Данные!I2, Таблицы!C35:J36", texts)
            self.assertIn("Для № 002B6 проверьте Данные!I3, Таблицы!C37:J38", texts)
            self.assertIn("Печатная вместимость", texts)
            self.assertEqual(texts.count("Что нужно исправить"), 1)
            self.assertFalse(main.can_export)
            preview.close()

    def test_equipment_preview_shows_real_modification_mismatch_and_workbook_location(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            seed_lab(main.storage)
            p = fixture(path / "equipment.xlsx", "ТЕСТ-СРЕДСТВО", "009B6", sticker="")
            main.protocols = [p]
            self.verify_protocols(main)
            item, = p.equipment
            day = date.fromisoformat(p.fields["verification_date"].value)
            client = FixtureClient(verifications=[{
                "vri_id": "1-111", "mit_number": item.type_number, "mi_number": item.serial,
                "mi_modification": "Другое исполнение А1", "verification_date": "2025-01-01",
                "valid_date": "2027-01-01", "applicability": True,
            }])
            main.lookups[lookup_key(item, day.isoformat())] = lookup_equipment(item, day, client)
            main.recheck()
            preview = ProtocolDialog(main)
            texts = "\n".join(label.text() for label in preview.findChildren(QLabel))
            self.assertEqual(texts.count("Модификация средства поверки расходится"), 1)
            for value in ("Учебное средство:", "Другое исполнение А1", "Оборудование!A2:E2"):
                self.assertIn(value, texts)
            self.assertNotIn("(VRI", texts)
            self.assertEqual(preview.equipment_models["mis"].rows[0][1][-1], "Не подтверждено")
            self.assertFalse(main.can_export)
            preview.close()

    def test_http_counts_for_reimport_settings_and_shared_equipment(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            main = self.window(path, online=True, registry_session=RegistrySession(opener=server.opener))
            seed_lab(main.storage)
            self.assertFalse(server.calls)
            file = path / "first.xlsx"
            second = path / "second.xlsx"
            fixture(file, "ТЕСТ-ПЕРВЫЙ", "001B6", sticker="")
            fixture(second, "ТЕСТ-ВТОРОЙ", "002B6", sticker="")
            main.import_paths([file])
            self.wait_for_import(main)
            self.assertTrue(main.can_export)
            self.assertEqual(sum(server.calls.values()), 3)
            main.source_model.set_all_checked(True)
            main.import_paths([file, second])
            self.wait_for_import(main)
            self.assertEqual(len(main.protocols), 2)
            self.assertTrue(main.can_export)
            self.assertEqual(sum(server.calls.values()), 3)
            self.assertEqual(main.source_model.marked_paths, {file})
            self.assertEqual(main.source.horizontalHeader().select_all.checkState(), Qt.CheckState.PartiallyChecked)

            main.source.selectRow(0)
            with patch.object(ProtocolDialog, "exec", return_value=0):
                main.show_results()
                main.source.doubleClicked.emit(main.source_model.index(1, 1))
            dialog = ProtocolDialog(main)
            self.assertEqual(dialog.instrument_model.rows[0][1][0], "001B6")
            dialog.selector.setCurrentIndex(1)
            texts = [label.text() for label in dialog.scroll.widget().findChildren(QLabel)]
            self.assertEqual(dialog.instrument_model.rows[0][1][0], "002B6")
            self.assertIn("Протокол ТЕСТ-ВТОРОЙ · ТСП/ТСМ", texts)
            dialog.close()
            apply_theme(self.app)
            with patch("arshin_generator.ui.main.laboratory_settings", return_value=False):
                main.settings_btn.click()
            main.recheck()
            self.assertEqual(sum(server.calls.values()), 3)
            self.assertEqual(main.source_model.marked_paths, {file})

            third = path / "third.xlsx"
            fixture(third, "ТЕСТ-НОВОЕ-СРЕДСТВО", "003B6", sticker="", equipment_serial="0002")
            main.import_paths([third])
            self.wait_for_import(main)
            self.assertTrue(main.can_export)
            self.assertEqual(sum(server.calls.values()), 4)
            self.assertTrue(any("mi_number=0002" in url for url in server.calls))
            fixture(file, "ТЕСТ-ПЕРВЫЙ", "001B6", sticker="", day=date(2027, 1, 2))
            main.import_paths([file])
            self.wait_for_import(main)
            self.assertEqual(main.source_model.marked_paths, {file})
            self.assertEqual(sum(server.calls.values()), 5)
            self.assertFalse(main.can_export)
            self.assertFalse(main.export_btn.isEnabled())
            self.assertEqual(main.source_model.rows[0][1][-1], "Требуется исправление")
            self.assertEqual(main.source_model.rows[1][1][-1], "Готов")
            details = ProtocolDialog(main, main.protocols[0])
            self.assertIn("Срок поверки средства истёк", "\n".join(label.text() for label in details.findChildren(QLabel)))
            self.assertEqual(details.equipment_models["mis"].rows[0][1][-1], "Не подтверждено")
            details.close()

    def test_expiry_is_passive_and_export_renews_only_stale_entries(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            ticks = [0]
            session = RegistrySession(clock=lambda: ticks[0], opener=server.opener)
            main = self.window(path, online=True, registry_session=session)
            seed_lab(main.storage)
            first = fixture(path / "first.xlsx", "ТЕСТ-1", "001B6", sticker="")
            second = fixture(path / "second.xlsx", "ТЕСТ-2", "002B6", sticker="", equipment_serial="002")
            main.import_paths([first.path])
            self.wait_for_import(main)
            self.assertEqual(sum(server.calls.values()), 3)
            ticks[0] = 450
            main.import_paths([second.path])
            self.wait_for_import(main)
            self.assertEqual(sum(server.calls.values()), 4)
            self.assertTrue(main.can_export)
            initial = server.calls.copy()

            ticks[0] = 900
            main.expiry_timer.start(1)
            QTest.qWait(20)
            self.assertFalse(main.can_export)
            self.assertTrue(main.export_btn.isEnabled())
            self.assertTrue(all(row[-1] == "Требуется повторная проверка" for _, row in main.source_model.rows))
            self.assertTrue(all(r.registry_pending and not r.data_issues for r in main.results))
            details = ProtocolDialog(main, main.protocols[0])
            texts = "\n".join(label.text() for label in details.scroll.widget().findChildren(QLabel))
            self.assertIn("Требуется повторная проверка", texts)
            self.assertNotIn("ещё не проверен", texts)
            self.assertNotIn("Проблемных", texts)
            self.assertNotIn("Что нужно исправить", texts)
            details.close()
            self.assertEqual(main.expiry_timer.interval(), 450_000)
            with patch.object(ProtocolDialog, "exec", return_value=0) as preview:
                main.open_protocol(main.protocols[0])
                main.open_protocol(main.protocols[1])
                self.assertEqual(preview.call_count, 2)
            self.assertEqual(server.calls, initial)

            output = path / "verified.xml"
            with patch.object(main, "choose_xml_path", return_value=output) as chooser:
                main.export_btn.click()
                self.assertTrue(main.busy)
                self.assertFalse(main.export_btn.isEnabled())
                chooser.assert_not_called()
                self.wait_for_import(main)
                chooser.assert_called_once()
            new_requests = server.calls - initial
            self.assertEqual(sum(new_requests.values()), 3)
            self.assertFalse(any("mi_number=002" in url for url in new_requests))
            root, issues, _ = open_xml(output, main.schema)
            self.assertEqual(len(root), 2)
            self.assertFalse(issues)

    def test_reload_reads_all_paths_and_keeps_marks_selection_and_fresh_cache(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            main = self.window(path, online=True, registry_session=RegistrySession(opener=server.opener))
            seed_lab(main.storage)
            files = [path / f"{i}.xlsx" for i in range(1, 4)]
            for index, file in enumerate(files, 1):
                fixture(file, f"ТЕСТ-{index}", f"00{index}B6", sticker="")
            main.import_paths(files)
            self.wait_for_import(main)
            self.assertTrue(main.can_export)
            self.assertEqual(sum(server.calls.values()), 3)
            model = main.source_model
            main.source.selectRow(0)
            model.setData(model.index(1, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
            overview = ProtocolDialog(main, main.protocols[0])

            fixture(files[0], "ТЕСТ-1-ИСПРАВЛЕН", "011B6", sticker="")
            files[1].unlink()
            files[2].write_bytes(b"bad workbook")
            before = files[0].read_bytes()
            main.recheck_btn.click()
            self.wait_for_import(main)
            self.assertEqual([p.path for p in main.protocols], files)
            self.assertEqual(main.protocols[0].instruments[0].serial, "011B6")
            self.assertEqual(overview.protocol, main.protocols[0])
            self.assertIn("ИСПРАВЛЕН", overview.selector.currentText())
            self.assertIn("недоступна", main.protocols[1].error)
            self.assertIn("не прочитана", main.protocols[2].error)
            self.assertEqual(model.marked_paths, {files[1]})
            self.assertEqual([i.row() for i in main.source.selectionModel().selectedRows()], [0])
            self.assertEqual(sum(server.calls.values()), 3)
            self.assertIn("Несохранённые изменения не учтены", main.notice.text())
            self.assertEqual(files[0].read_bytes(), before)
            self.assertFalse(main.can_export)
            overview.close()

    def test_year_editor_saves_batch_and_individual_years_then_exports_after_reload(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            main = self.window(path, online=True, registry_session=RegistrySession(opener=server.opener))
            seed_lab(main.storage)
            file = path / "batch.xlsx"
            fixture(file, "ТЕСТ-ГОД", "", count=2, dual=True, sticker="")
            main.import_paths([file])
            self.wait_for_import(main)
            preview = ProtocolDialog(main)
            self.assertEqual(preview.year_input.text(), "2026")
            self.assertIn("Предполагаемый год — год поверки.",
                          [label.text() for label in preview.scroll.widget().findChildren(QLabel)])

            def save(year):
                preview.year_input.setText(year)
                next(btn for btn in preview.scroll.widget().findChildren(QPushButton)
                     if btn.text() == "Сохранить год").click()

            save("2024")
            preview.year_selector.setCurrentIndex(preview.year_selector.findData("002B6"))
            self.assertEqual(preview.year_input.text(), "2024")
            for invalid in ("2027", "20х5"):
                save(invalid)
                self.assertFalse(main.can_export)
                self.assertIn("Год изготовления должен", " ".join(main.results[1].blockers))
                self.assertFalse(main.results[0].data_issues)
            save("2025")
            self.assertTrue(main.can_export)
            preview.close()
            fixture(file, "ТЕСТ-ГОД", "", count=2, dual=True, sticker="", owner="Другой владелец")
            main.recheck_btn.click()
            self.wait_for_import(main)
            self.assertEqual([r.value("manufacture_year") for r in main.results], ["2024", "2025"])
            self.assertEqual(sum(server.calls.values()), 3)
            out = path / "years.xml"
            with patch.object(main, "choose_xml_path", return_value=out):
                main.export_btn.click()
            root, issues, _ = open_xml(out, main.schema)
            self.assertFalse(issues)
            self.assertEqual([node.text for node in root.findall(f".//{{{NS}}}manufactureYear")], ["2024", "2025"])

    def test_protocol_table_aligns_each_header_with_its_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            main.protocols = [fixture(path / "long.xlsx", "Протокол " + "Х" * 190, "001B6")]
            main.recheck()
            main.show()
            header = main.source.horizontalHeader()
            model = main.source_model
            for width in (740, 1000):
                main.resize(width, 600)
                self.app.processEvents()
                for column, horizontal in enumerate((
                    Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignLeft,
                    Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignHCenter,
                )):
                    alignment = horizontal | Qt.AlignmentFlag.AlignVCenter
                    self.assertEqual(model.index(0, column).data(Qt.ItemDataRole.TextAlignmentRole), alignment)
                    self.assertEqual(model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole),
                                     alignment)
                self.assertLessEqual(abs(header.select_all.geometry().center().x() - header.sectionSize(0) // 2), 1)
                self.assertEqual(main.source.columnWidth(3), 80)
                self.assertEqual(main.source.rowHeight(0), 36)

    def test_expiry_during_save_dialog_preserves_destination_and_continues(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            ticks = [0]
            session = RegistrySession(clock=lambda: ticks[0], opener=server.opener)
            main = self.window(path, online=True, registry_session=session)
            seed_lab(main.storage)
            protocol = fixture(path / "input.xlsx", "ТЕСТ-ДИАЛОГ", "001B6", sticker="")
            main.import_paths([protocol.path])
            self.wait_for_import(main)
            output = path / "after dialog.xml"

            def choose(_):
                ticks[0] += 900
                return output

            with patch.object(main, "choose_xml_path", side_effect=choose) as chooser:
                main.export_btn.click()
                self.assertFalse(output.exists())
                self.wait_for_import(main)
                chooser.assert_called_once()
            self.assertTrue(output.is_file())
            self.assertEqual(sum(server.calls.values()), 6)

    def test_export_retry_after_connection_error_and_http_429(self):
        for failure, attempts, http_requests in (("connection", 3, 0), ("rate", 1, 1)):
            with self.subTest(failure=failure), TemporaryDirectory() as directory, registry_http() as server:
                path = Path(directory)
                ticks = [0]
                session = RegistrySession(clock=lambda: ticks[0], opener=server.opener)
                main = self.window(path, online=True, registry_session=session)
                seed_lab(main.storage)
                protocol = fixture(path / "input.xlsx", "ТЕСТ-СБОЙ", "001B6", sticker="")
                main.import_paths([protocol.path])
                self.wait_for_import(main)
                self.assertTrue(main.can_export)
                ticks[0] = 900
                before = server.calls.copy()
                server.failure = 429 if failure == "rate" else 0

                def opener(request, *args, **kwargs):
                    if failure == "connection" and "/vri?" in request.full_url:
                        raise URLError("Соединение отсутствует")
                    return server.opener(request, *args, **kwargs)

                output = path / "retry.xml"
                with patch.object(session, "opener", side_effect=opener) as requests, \
                        patch("arshin_generator.public_registry.sleep") as delay, \
                        patch.object(main, "choose_xml_path", return_value=output) as chooser:
                    main.export_btn.click()
                    self.wait_for_import(main)
                    chooser.assert_not_called()
                    self.assertFalse(output.exists())
                    self.assertFalse(main.can_export)
                    self.assertTrue(main.export_btn.isEnabled())
                    self.assertIn("Не удалось завершить проверку", main.notice.text())
                    self.assertEqual(sum("/vri?" in call.args[0].full_url for call in requests.call_args_list), attempts)
                    self.assertEqual(sum((server.calls - before).values()), 2 + http_requests)
                    self.assertEqual(delay.call_count, 2 if failure == "connection" else 0)
                    before = server.calls.copy()
                    server.failure = 0
                    requests.side_effect = server.opener
                    main.export_btn.click()
                    self.wait_for_import(main)
                    chooser.assert_called_once()
                    self.assertTrue(output.exists())
                    self.assertEqual(sum((server.calls - before).values()), 1)

    def test_single_missing_excel_fields_block_before_any_http(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            main = self.window(path, online=True, registry_session=RegistrySession(opener=server.opener))
            seed_lab(main.storage)
            file = path / "unchecked.xlsx"
            for fields, cell in (({"temperature": None}, "B5"), ({"kind": None}, "B1")):
                with self.subTest(cell=cell):
                    fixture(file, "ТЕСТ-ПОЛЕ", "001B6", sticker="", **fields)
                    main.import_paths([file])
                    self.wait_for_import(main)
                    self.assertEqual(main.source_model.rows[0][1][-1], "Требуется исправление")
                    details = ProtocolDialog(main, main.protocols[0])
                    self.assertIn(f"ячейку {cell}", "\n".join(label.text() for label in details.findChildren(QLabel)))
                    details.close()
                    self.assertFalse(main.can_export)
                    self.assertFalse(server.calls)

    def test_preview_keeps_reader_name_and_uses_exported_modification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            seed_lab(main.storage)
            first = fixture(path / "rtd.xlsx", "ТЕСТ-ТСП", "001B6", execution=None, ex=None, sticker="")
            second = fixture(path / "other.xlsx", "ТЕСТ-ДРУГОЙ", "002B6", sticker="")
            second.reader_name = "Тестовый формат"
            main.protocols = [first, second]
            self.verify_protocols(main)
            self.assertEqual([row[1][2] for row in main.source_model.rows], ["ТСП/ТСМ", "Тестовый формат"])
            preview = ProtocolDialog(main, first)
            texts = [label.text() for label in preview.scroll.widget().findChildren(QLabel)]
            self.assertIn("Протокол ТЕСТ-ТСП · ТСП/ТСМ", texts)
            self.assertIn("ТСП-0193", texts)
            self.assertNotIn("Исполнение", texts)
            self.assertNotIn("Взрывозащита", texts)
            preview.selector.setCurrentIndex(1)
            texts = [label.text() for label in preview.scroll.widget().findChildren(QLabel)]
            self.assertIn("Протокол ТЕСТ-ДРУГОЙ · Тестовый формат", texts)
            self.assertIn("Модификация СИ", texts)
            self.assertNotIn("Модель и модификация", texts)
            preview.close()

    def test_existing_history_status_action_keeps_documentary_confirmation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            main.storage.record_export("batch", path, ["test.xml"], SCHEMA_HASH, "Тест", [], "Тест", [])
            history = HistoryDialog(main, main.storage, main.schema)
            confirm = next(widget for widget in history.findChildren(QPushButton)
                           if widget.text() == "Подтвердить статус в кабинете")

            def submit(form):
                form.editor.controls["status"].setCurrentText("Загружен")
                with self.assertRaises(ValueError):
                    form.validate(form.values())
                form.editor.controls["evidence"].setText("Заявка 123")
                form.editor.controls["confirmer"].setText("Сотрудник")
                form.validate(form.values())
                return QDialog.DialogCode.Accepted

            with patch.object(Form, "exec", new=submit):
                confirm.click()
            self.assertEqual(main.storage.history()[0]["status"], "Загружен")
            history.close()

    def test_import_audit_failure_keeps_queue_and_database_consistent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            first = fixture(path / "first.xlsx", "ТЕСТ-1", "001B6")
            second = fixture(path / "second.xlsx", "ТЕСТ-2", "002B6")
            original = main.storage._audit

            def audit(db, event, data):
                if data["number"] == second.number:
                    raise sqlite3.OperationalError("Испытание сбоя записи")
                original(db, event, data)

            with patch.object(main.storage, "_audit", side_effect=audit):
                main.import_paths([first.path, second.path])
                self.wait_for_import(main)
            self.assertFalse(main.protocols)
            self.assertIn("Не удалось записать историю импорта", main.notice.text())
            with main.storage.session() as db:
                self.assertEqual(db.execute("select count(*) from audit").fetchone()[0], 0)

    def test_native_save_configuration_and_split_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            save_as = path / "Выбранное имя.xml"

            def accept(dialog):
                self.assertEqual(dialog.acceptMode(), QFileDialog.AcceptMode.AcceptSave)
                self.assertEqual(dialog.fileMode(), QFileDialog.FileMode.AnyFile)
                self.assertFalse(dialog.testOption(QFileDialog.Option.DontUseNativeDialog))
                self.assertFalse(dialog.testOption(QFileDialog.Option.DontConfirmOverwrite))
                self.assertFalse(self.app.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs))
                self.assertEqual(dialog.defaultSuffix(), "xml")
                return QDialog.DialogCode.Accepted

            with patch.object(QFileDialog, "exec", new=accept), \
                    patch.object(QFileDialog, "selectFile") as suggested, \
                    patch.object(QFileDialog, "selectedFiles", return_value=[str(save_as)]):
                self.assertEqual(main.choose_xml_path(path / "Предложенное.xml"), save_as)
                suggested.assert_called_once_with("Предложенное.xml")

            for i in range(1, 3):
                protocol = fixture(path / f"{i}.xlsx", f"ТЕСТ-{i}", f"{i:03d}B6", sticker="")
                seed_lab(main.storage)
                main.protocols.append(protocol)
            self.verify_protocols(main)
            limit = max(len(format_packets([r], main.schema)[0][0]) for r in main.results)
            with patch("arshin_generator.ui.main.format_packets", side_effect=lambda r, s: format_packets(r, s, limit)), \
                    patch.object(main, "choose_xml_path") as dialog, \
                    patch.object(QFileDialog, "getExistingDirectory", return_value=str(path)) as chooser:
                main.export_xml()
                dialog.assert_not_called()
                chooser.assert_called_once()
            [entry] = main.storage.history()
            names = json.loads(entry["files"])
            stem = f"СИ-Экспорт_{date.today().isoformat()}"
            self.assertEqual(names, [f"{stem}_001.xml", f"{stem}_002.xml"])
            self.assertEqual(entry["directory"], str(path))
            for name in names:
                root, issues, _ = open_xml(path / name, main.schema)
                self.assertEqual(len(root), 1)
                self.assertFalse(issues)
            main.close()

    def test_save_settings_reopen_without_lab_audit_or_registry_check(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path, online=True)
            automatic = path / "Автоматическая"
            automatic.mkdir()
            previous = path / "Ручная"
            previous.mkdir()
            main.storage.set_setting("output_folder", str(previous))
            self.assertEqual(main.storage.xml_output(), (False, ""))
            self.assertEqual(main.output_folder(), previous)

            def configure(form):
                manual, auto = form.findChildren(QRadioButton)
                self.assertTrue(manual.isChecked())
                choose = next(widget for widget in form.findChildren(QPushButton) if widget.text() == "Обзор…")
                self.assertFalse(choose.isEnabled())
                auto.click()
                self.assertTrue(choose.isEnabled())
                choose.click()
                form._accept()
                return form.result()

            with patch.object(Form, "exec", new=configure), \
                    patch.object(QFileDialog, "getExistingDirectory", return_value=str(automatic)) as directory_dialog, \
                    patch.object(main, "recheck") as recheck, \
                    patch.object(main, "start_registry_checks") as registry:
                main.settings_btn.click()
                directory_dialog.assert_called_once()
                recheck.assert_not_called()
                registry.assert_not_called()
            main.close()
            reopened = self.window(path)
            self.assertEqual(reopened.storage.xml_output(), (True, str(automatic)))
            self.assertEqual(reopened.output_folder(), previous)
            self.assertEqual(reopened.storage.laboratory(), {})
            with reopened.storage.session() as db:
                self.assertEqual(db.execute("select count(*) from audit").fetchone()[0], 0)

            def switch_to_manual(form):
                manual, auto = form.findChildren(QRadioButton)
                self.assertTrue(auto.isChecked())
                manual.click()
                form._accept()
                return form.result()

            with patch.object(Form, "exec", new=switch_to_manual):
                reopened.settings_btn.click()
            self.assertEqual(reopened.storage.xml_output(), (False, str(automatic)))
            self.assertEqual(reopened.output_folder(), previous)

    def test_automatic_single_export_uses_next_unused_name_and_keeps_manual_folder(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            seed_lab(main.storage)
            main.protocols = [fixture(path / "first.xlsx", "ТЕСТ-1", "001B6", sticker="")]
            self.verify_protocols(main)
            old_path = path / "СИ-Экспорт_ТЕСТ-1.xml"
            old_path.write_bytes(b"existing file")
            main.storage.save_xml_output(True, str(path))
            manual = path / "Последняя ручная папка"
            manual.mkdir()
            main.storage.set_setting("output_folder", str(manual))
            with patch.object(main, "choose_xml_path") as single_dialog, \
                    patch.object(QFileDialog, "getExistingDirectory") as folder_dialog, \
                    patch("arshin_generator.ui.main.message") as prompt:
                main.export_btn.click()
                main.export_btn.click()
                single_dialog.assert_not_called()
                folder_dialog.assert_not_called()
                prompt.assert_not_called()
            for number in (2, 3):
                xml = path / f"СИ-Экспорт_ТЕСТ-1_{number}.xml"
                root, issues, _ = open_xml(xml, main.schema)
                self.assertEqual(len(root), 1)
                self.assertFalse(issues)
            self.assertEqual(old_path.read_bytes(), b"existing file")
            self.assertEqual(len(main.storage.history()), 2)
            self.assertEqual(main.output_folder(), manual)
            self.assertIn("СИ-Экспорт_ТЕСТ-1_3.xml", main.notice.text())

    def test_automatic_split_reserves_all_parts_and_retries_atomic_collision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            seed_lab(main.storage)
            main.protocols = [fixture(path / f"{i}.xlsx", f"ТЕСТ-{i}", f"00{i}B6", sticker="") for i in (1, 2)]
            self.verify_protocols(main)
            main.storage.save_xml_output(True, str(path))
            stem = f"СИ-Экспорт_{date.today().isoformat()}"
            old_part = path / f"{stem}_009.xml"
            old_part.write_bytes(b"existing series")
            limit = max(len(format_packets([r], main.schema)[0][0]) for r in main.results)

            def collide(packets, schema, paths, storage, confirmer):
                if paths[0].name == f"{stem}_2_001.xml":
                    paths[1].write_bytes(b"created meanwhile")
                return export(packets, schema, paths, storage, confirmer)

            with patch("arshin_generator.ui.main.format_packets", side_effect=lambda r, s: format_packets(r, s, limit)), \
                    patch("arshin_generator.ui.main.export", side_effect=collide) as write, \
                    patch.object(QFileDialog, "getExistingDirectory") as dialog:
                main.export_btn.click()
                self.assertEqual(write.call_count, 2)
                dialog.assert_not_called()
            self.assertEqual(old_part.read_bytes(), b"existing series")
            self.assertFalse((path / f"{stem}_2_001.xml").exists())
            self.assertEqual((path / f"{stem}_2_002.xml").read_bytes(), b"created meanwhile")
            [entry] = main.storage.history()
            names = json.loads(entry["files"])
            self.assertEqual(names, [f"{stem}_3_001.xml", f"{stem}_3_002.xml"])
            for filename in names:
                root, issues, _ = open_xml(path / filename, main.schema)
                self.assertEqual(len(root), 1)
                self.assertFalse(issues)
            self.assertIn("файлов 2", main.notice.text())
            self.assertIn(str(path), main.notice.text())

    def test_automatic_folder_fallback_is_explicit_and_does_not_change_settings(self):
        for failure in ("missing", "no_space", "permission"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                path = Path(directory)
                main = self.window(path)
                seed_lab(main.storage)
                main.protocols = [fixture(path / "first.xlsx", "ТЕСТ-1", "001B6", sticker="")]
                self.verify_protocols(main)
                unavailable = path / "Постоянная"
                temporary = path / "Разовая"
                temporary.mkdir()
                if failure != "missing":
                    unavailable.mkdir()
                main.storage.save_xml_output(True, str(unavailable))
                main.storage.set_setting("output_folder", str(path))

                def write(packets, schema, paths, storage, confirmer):
                    if paths[0].parent == unavailable:
                        if failure == "no_space":
                            raise OSError(ENOSPC, "Недостаточно места")
                        raise PermissionError("Папка недоступна на запись")
                    return export(packets, schema, paths, storage, confirmer)

                with patch("arshin_generator.ui.main.export", side_effect=write), \
                        patch("arshin_generator.ui.main.message", return_value=False) as prompt, \
                        patch.object(QFileDialog, "getExistingDirectory", return_value=str(temporary)) as chooser:
                    main.export_btn.click()
                    prompt.assert_called_once()
                    chooser.assert_not_called()
                    self.assertFalse(main.storage.history())
                    self.assertEqual(list(temporary.iterdir()), [])
                    prompt.return_value = True
                    main.export_btn.click()
                    chooser.assert_called_once()
                [entry] = main.storage.history()
                self.assertEqual(entry["directory"], str(temporary))
                xml = temporary / "СИ-Экспорт_ТЕСТ-1.xml"
                _, issues, _ = open_xml(xml, main.schema)
                self.assertFalse(issues)
                self.assertEqual(main.storage.xml_output(), (True, str(unavailable)))
                self.assertEqual(main.storage.setting("output_folder"), str(path))

    def test_automatic_export_continues_after_refreshing_registry(self):
        with TemporaryDirectory() as directory, registry_http() as server:
            path = Path(directory)
            ticks = [0]
            session = RegistrySession(clock=lambda: ticks[0], opener=server.opener)
            main = self.window(path, online=True, registry_session=session)
            seed_lab(main.storage)
            main.storage.save_xml_output(True, str(path))
            protocol = fixture(path / "first.xlsx", "ТЕСТ-1", "001B6", sticker="")
            main.import_paths([protocol.path])
            self.wait_for_import(main)
            self.assertEqual(sum(server.calls.values()), 3)
            ticks[0] += 900
            xml = path / "СИ-Экспорт_ТЕСТ-1.xml"
            with patch.object(main, "choose_xml_path") as file_dialog, \
                    patch.object(QFileDialog, "getExistingDirectory") as folder_dialog:
                main.export_btn.click()
                self.assertFalse(xml.exists())
                self.assertFalse(main.storage.history())
                self.wait_for_import(main)
                file_dialog.assert_not_called()
                folder_dialog.assert_not_called()
            self.assertEqual(sum(server.calls.values()), 6)
            root, issues, _ = open_xml(xml, main.schema)
            self.assertEqual(len(root), 1)
            self.assertFalse(issues)
            self.assertEqual(len(main.storage.history()), 1)


    def test_ambiguous_mieta_can_only_be_corrected_in_excel(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            protocol = fixture(path / "ui.xlsx", "ТЕСТ-КАРТОЧКИ", "001B6")
            item = etalon()
            protocol.equipment = [item]
            main.protocols = [protocol]
            main.type_titles["56560-14"] = "Термопреобразователи сопротивления"
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
            key = lookup_key(item, day.isoformat())
            main.lookups[key] = found
            main.recheck()
            dialog = ProtocolDialog(main, protocol)
            texts = "\n".join(label.text() for label in dialog.scroll.widget().findChildren(QLabel))
            self.assertEqual(texts.count("Не удалось однозначно подтвердить запись"), 1)
            self.assertIn(item.source, texts)
            self.assertNotIn(first[1], texts)
            self.assertNotIn(second[1], texts)
            self.assertNotIn("Выбрать запись", [button.text() for button in dialog.findChildren(QPushButton)])
            self.assertEqual(len(dialog.scroll.widget().findChildren(QTableView)), 2)
            self.assertEqual(dialog.equipment_models["mieta"].rows[0][1][-1], "Не подтверждено")
            self.assertFalse(main.can_export)
            self.assertFalse(main.storage.entries())
            main.lookups[key] = EquipmentLookup("choice", "В номере эталона из Excel латинская P. Сверьте карточку с кириллической Р",
                                                (found.candidates[0],))
            main.recheck()
            texts = "\n".join(label.text() for label in dialog.scroll.widget().findChildren(QLabel))
            self.assertEqual(texts.count("латинская P вместо кириллической Р"), 1)
            self.assertFalse(main.can_export)
            dialog.close()

    def test_protocol_preview_shows_all_values_emitted_to_xml(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            method = "ГОСТ 8.461-2009 ГСИ. Термопреобразователи сопротивления. Методика поверки для испытаний"
            good = fixture(path / "good.xlsx", "Первичный", "001B6", method=method)
            bad = fixture(path / "bad.xlsx", "Периодический", "002B6", outcome="НЕ СООТВЕТСТВУЕТ", kind="периодической")
            for key, value in (("structure", "Три части"), ("brief", "Проверен указанный диапазон"),
                               ("additional", "Контрольные сведения")):
                good.fields[key] = Datum(value, "Данные испытания")
            main.protocols = [good, bad]
            self.verify_protocols(main)
            self.assertTrue(main.can_export)
            dialog = ProtocolDialog(main, good)
            [good_xml, bad_xml], errors, _ = main.schema.inspect(format_packets(main.results, main.schema)[0][0])
            self.assertFalse(errors)
            fields = preview_fields(dialog)
            tags = {
                "Модификация СИ": "modification", "Условный шифр знака поверки": "signCipher",
                "Владелец СИ": "miOwner", "Документ на методику поверки": "docTitle", "ФИО поверителя": "metrologist",
                "Номер наклейки": "stickerNum", "Температура": "temperature", "Атмосферное давление": "pressure",
                "Относительная влажность": "hymidity", "Другие факторы": "other",
                "Состав СИ, представленного на поверку": "structure", "Краткая характеристика объема поверки": "characteristics",
                "Прочие сведения": "additional_info",
            }
            for title, tag in tags.items():
                with self.subTest(field=title):
                    self.assertEqual(fields[title], good_xml.findtext(f".//{{{NS}}}{tag}"))
            for title, tag in (("Использование результатов калибровки", "calibration"),
                               ("Знак поверки в паспорте", "signPass"), ("Знак поверки на СИ", "signMi")):
                self.assertEqual(fields[title], {"false": "Нет", "true": "Да"}[good_xml.findtext(f".//{{{NS}}}{tag}")])
            for title, tag in (("Дата поверки", "vrfDate"), ("Действительна до", "validDate")):
                self.assertEqual(fields[title], date.fromisoformat(good_xml.findtext(f"{{{NS}}}{tag}")).strftime("%d.%m.%Y"))
            self.assertEqual(fields["Поверка в сокращенном объеме"], "Да")
            self.assertEqual(fields["Тип поверки"], "Первичная")
            self.assertEqual(good_xml.findtext(f"{{{NS}}}type"), "1")
            self.assertEqual(fields["Условный шифр знака поверки"], "ВЦЯ")
            self.assertEqual(fields["ФИО поверителя"], "Тестова А.В.")
            self.assertEqual(fields["Тип СИ в реестре ФИФ ОЕИ"], "56560-14 · Термопреобразователи сопротивления")
            self.assertEqual(dialog.instrument_model.headers, ("Заводской №", "Год выпуска", "Результат поверки"))
            self.assertEqual(dialog.instrument_model.rows[0][1], (good_xml.findtext(f".//{{{NS}}}manufactureNum"),
                             good_xml.findtext(f".//{{{NS}}}manufactureYear"), "Пригодно"))

            dialog.selector.setCurrentIndex(1)
            fields = preview_fields(dialog)
            self.assertEqual(fields["Тип поверки"], "Периодическая")
            self.assertEqual(bad_xml.findtext(f"{{{NS}}}type"), "2")
            self.assertEqual(fields["Причины непригодности"], bad_xml.findtext(f".//{{{NS}}}reasons"))
            self.assertEqual(dialog.instrument_model.rows[0][1], ("002B6", None, "Непригодно"))
            for title in ("Действительна до", "Номер наклейки", "Знак поверки в паспорте", "Знак поверки на СИ",
                          "Поверка в сокращенном объеме", "Состав СИ, представленного на поверку", "Прочие сведения"):
                self.assertNotIn(title, fields)
            self.assertIsNone(bad_xml.find(f"{{{NS}}}validDate"))
            for record in (good_xml, bad_xml):
                self.assertFalse(any("certificate" in node.tag.lower() for node in record.iter()))
            texts = [widget.text() for widget in dialog.scroll.widget().findChildren(QLabel)]
            self.assertNotIn("№ свидетельства", texts)
            dialog.close()

    def test_settings_default_edit_restart_and_preserve_saved_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            p = fixture(path / "original.xlsx", "Один", "001B6", sticker="")
            main.protocols = [p]
            self.verify_protocols(main)
            dialog = ProtocolDialog(main, p)

            def edit(form):
                self.assertEqual(form.values(), {"sign_cipher": "ВЦЯ", "calibration": False})
                cipher = form.editor.controls["sign_cipher"]
                calibration = form.editor.controls["calibration"]
                self.assertEqual(calibration.currentText(), "Нет")
                cipher.setText("ТСТ")
                calibration.setCurrentText("Да")
                form._accept()
                return form.result()

            with patch.object(Form, "exec", new=edit):
                main.settings_btn.click()
            self.assertEqual(preview_fields(dialog)["Условный шифр знака поверки"], "ТСТ")
            self.assertEqual(preview_fields(dialog)["Использование результатов калибровки"], "Да")
            xml = result_element(main.results[0])
            self.assertEqual(xml.findtext(f"{{{NS}}}signCipher"), "ТСТ")
            self.assertEqual(xml.findtext(f"{{{NS}}}calibration"), "true")
            dialog.close()
            main.close()

            reopened = self.window(path)
            self.assertEqual(reopened.storage.laboratory(), {"sign_cipher": "ТСТ", "calibration": True})
            p.fields["sign_cipher"] = Datum("ЛАБ", "Таблицы!F22")
            p.fields["calibration"] = Datum(False, "Таблицы!F21")
            reopened.protocols = [p]
            self.verify_protocols(reopened)
            xml = result_element(reopened.results[0])
            self.assertEqual(xml.findtext(f"{{{NS}}}signCipher"), "ЛАБ")
            self.assertEqual(xml.findtext(f"{{{NS}}}calibration"), "false")

            # A pre-existing database with only one saved field receives the new fallback for the other.
            reopened.storage.save_laboratory({"sign_cipher": "МВ"}, "Тест")
            for key in ("sign_cipher", "calibration"):
                p.fields.pop(key)
            reopened.recheck()
            self.assertEqual(reopened.storage.laboratory(), {"sign_cipher": "МВ"})
            xml = result_element(reopened.results[0])
            self.assertEqual(xml.findtext(f"{{{NS}}}signCipher"), "МВ")
            self.assertEqual(xml.findtext(f"{{{NS}}}calibration"), "false")


if __name__ == "__main__":
    unittest.main()
