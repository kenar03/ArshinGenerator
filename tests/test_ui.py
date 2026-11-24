import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from openpyxl import load_workbook
from PySide6.QtCore import QEventLoop, QItemSelectionModel, QPoint, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QLabel, QLineEdit, QPushButton, QTableView, QTreeWidget,
)

from arshin_generator.arshin import NS, SCHEMA_HASH, Schema, format_packets, open_xml, result_element, signature
from arshin_generator.confirmations import protocol_key
from arshin_generator.domain import PreviewField, RegistryEntry
from arshin_generator.readers import read_protocol
from arshin_generator.storage import Storage
from arshin_generator.registry import EquipmentCandidate, EquipmentLookup, lookup_equipment, lookup_key, resolve_many
from arshin_generator.ui.common import Form
from arshin_generator.ui.dialogs import EquipmentReviewPanel, HistoryDialog, XmlDialog
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
            self.assertEqual(overview.equipment_panel.checked, 7)
            self.assertFalse(overview.findChildren(QLineEdit))
            texts = "\n".join(label.text() for label in overview.findChildren(QLabel))
            self.assertIn("56560-14 · Термопреобразователи сопротивления", texts)
            self.assertIn("Регистрационный номер типа СИ", texts)
            self.assertIn("Исполнение", texts)
            self.assertNotIn("Модель и модификация", texts)
            self.assertIn("Готов к выгрузке", texts)
            self.assertIn('ООО "ЧТП"', texts)
            self.assertIn("Еремеева А.В.", texts)
            self.assertIn(SAMPLE_METHOD, texts)
            self.assertIn("22.11.2026", texts)
            self.assertIn("21.11.2030", texts)
            self.assertIn("Приборов: 25 · Пригодны: 25 · Непригодны: 0", texts)
            self.assertIn("009W6–033W6 (25 приборов)", texts)
            self.assertNotIn("Непригодные приборы", texts)
            self.assertIn("Средства поверки: проверено 7 из 7", texts)
            self.assertEqual(len([v for v in overview.findChildren(QTableView) if not v.isHidden()]), 1)
            overview.serial_toggle.click()
            self.assertEqual(overview.serial_toggle.text(), "Свернуть")
            full = next(label for label in overview.findChildren(QLabel) if label.text().startswith("009W6, "))
            self.assertFalse(full.isHidden())
            preview_serials = full.text().split(", ")
            overview.serial_toggle.click()
            self.assertTrue(full.isHidden())
            reviewed_equipment = [row[1][2] for row in overview.equipment_panel.item_model.rows]
            self.assertIn("32777.06.2Р.01504903", reviewed_equipment)
            overview.close()

            with patch.object(main, "choose_xml_path", return_value=file) as dialog, \
                    patch("arshin_generator.ui.main.message") as prompt:
                main.export_btn.click()
                self.assertEqual(dialog.call_args.args[0].name, "Аршин_П-Т1-з2-1260-26.xml")
                prompt.assert_not_called()
            self.assertEqual(main.notice.text(), "XML сохранён: 25 записей")
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
            index = texts.index("Непригодные приборы")
            self.assertEqual(texts[index + 1], "002B6")
            self.assertIn("001B6, 002B6", texts)
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
            self.assertLess(len(labels), 40)
            self.assertIn("003412, 005412, 1548-3-1 и ещё 497", [label.text() for label in labels])
            dialog.serial_toggle.click()
            self.app.processEvents()
            full = next(label for label in labels if label.text().startswith("003412, 005412, 1548-3-1, "))
            self.assertTrue(full.isVisible())
            numbers = full.text().split(", ")
            self.assertEqual(len(numbers), 500)
            self.assertEqual(numbers[3], "900В6 (в XML: 900B6)")
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
            self.assertFalse(overview.findChildren(QLineEdit))
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
                             pos=QPoint(rect.left() + 10, rect.center().y()))
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
            self.assertEqual(main.source_model.rows[0][1][-1], "Требует проверки")
            type_dialog = ProtocolDialog(main, p)
            self.assertIn("не найден точный тип СИ", "\n".join(label.text() for label in type_dialog.findChildren(QLabel)))
            type_dialog.close()
            self.assertFalse(main.can_export)
            item = p.equipment[0]
            key = lookup_key(item, p.fields["verification_date"].value)
            main.type_titles["56560-14"] = "Термопреобразователи сопротивления"
            main.lookups[key] = EquipmentLookup("unavailable", "Не удалось подключиться к реестру")
            main.recheck()
            self.assertEqual(main.source_model.rows[0][1][-1], "Требует проверки")
            self.assertFalse(main.can_export)
            dialog = ProtocolDialog(main, p)
            self.assertEqual(dialog.equipment_panel.checked, 0)
            self.assertIn("Не удалось подключиться к реестру", dialog.equipment_panel.item_model.rows[0][1][-1])
            self.assertIn("Повторить проверку", [btn.text() for btn in dialog.findChildren(QPushButton)])
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
            self.assertFalse(dialog.findChildren(QLineEdit))
            dialog.close()
            main.close()

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
            self.assertIn("001B6", [label.text() for label in dialog.scroll.widget().findChildren(QLabel)])
            dialog.selector.setCurrentIndex(1)
            texts = [label.text() for label in dialog.scroll.widget().findChildren(QLabel)]
            self.assertIn("002B6", texts)
            self.assertIn("Протокол ТЕСТ-ВТОРОЙ", texts)
            self.assertNotIn("001B6", texts)
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
            self.assertEqual(main.source_model.rows[0][1][-1], "Требует проверки")
            self.assertEqual(main.source_model.rows[1][1][-1], "Готов")
            details = ProtocolDialog(main, main.protocols[0])
            self.assertIn("Срок поверки средства истёк", details.equipment_panel.item_model.rows[0][1][-1])
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
                    self.assertEqual(main.source_model.rows[0][1][-1], "Ошибка")
                    details = ProtocolDialog(main, main.protocols[0])
                    self.assertIn(f"ячейку {cell}", "\n".join(label.text() for label in details.findChildren(QLabel)))
                    details.close()
                    self.assertFalse(main.can_export)
                    self.assertFalse(server.calls)

    def test_preview_uses_reader_labels_and_hides_empty_marking_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            main = self.window(path)
            seed_lab(main.storage)
            first = fixture(path / "rtd.xlsx", "ТЕСТ-ТСП", "001B6", execution=None, ex=None, sticker="")
            second = fixture(path / "other.xlsx", "ТЕСТ-ДРУГОЙ", "002B6", sticker="")
            second.reader_name = "Тестовый формат"
            second.preview_fields = (PreviewField("model", "Обозначение испытаний"),)
            main.protocols = [first, second]
            self.verify_protocols(main)
            self.assertEqual([row[1][2] for row in main.source_model.rows], ["ТСП/ТСМ", "Тестовый формат"])
            preview = ProtocolDialog(main, first)
            texts = [label.text() for label in preview.scroll.widget().findChildren(QLabel)]
            self.assertIn("ТСП/ТСМ", texts)
            self.assertIn("ТСП-0193", texts)
            self.assertNotIn("Исполнение", texts)
            self.assertNotIn("Взрывозащита", texts)
            preview.selector.setCurrentIndex(1)
            texts = [label.text() for label in preview.scroll.widget().findChildren(QLabel)]
            self.assertIn("Тестовый формат", texts)
            self.assertIn("Обозначение испытаний", texts)
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
            stem = f"Аршин_{date.today().isoformat()}"
            self.assertEqual(names, [f"{stem}_001.xml", f"{stem}_002.xml"])
            self.assertEqual(entry["directory"], str(path))
            for name in names:
                root, issues, _ = open_xml(path / name, main.schema)
                self.assertEqual(len(root), 1)
                self.assertFalse(issues)
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
