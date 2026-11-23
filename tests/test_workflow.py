from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

from arshin_generator.arshin import NS, Schema, export, format_packets, open_xml, signature
from arshin_generator.domain import Datum, RegistryEntry
from arshin_generator.normalization import modification_candidate, metrologist, serial_list
from arshin_generator.public_registry import exact_records
from arshin_generator.readers import read_protocol
from arshin_generator.registry import iso_date, resolve
from arshin_generator.storage import Storage
from arshin_generator.validation import check, individual_key, modification_key, protocol_key

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "Файлы для задания"
SAMPLE = MATERIALS / "П-Т1-з2-1260-26.xlsm"
TEMPLATE = MATERIALS / "П-Т1-з2-!!!!!-26.xltm"
JOURNAL = MATERIALS / "Журнал.xlsx"
XML_EXAMPLE = MATERIALS / "arshin_export_example.xml"
SCHEMA_HASH = "71843c9544546ce2a319f14f8fd645c6d18c080ad73007070815e2a33e2c5911"


class NormalizationTests(unittest.TestCase):
    def test_ranges_and_lists(self):
        examples = {
            "№009W6–033W6": [f"{i:03d}W6" for i in range(9, 34)],
            "058-059M6": ["058M6", "059M6"],
            "900В6-927В6": [f"{i}B6" for i in range(900, 928)],
            "005-014": [f"{i:03d}" for i in range(5, 15)],
            "227-266А6 268-293А6": [f"{i}A6" for i in range(227, 294) if i != 267],
            "243,246-247А6": ["243", "246A6", "247A6"],
            "419R6, 422R6": ["419R6", "422R6"],
            "1548-3-1": ["1548-3-1"],
            "0010538": ["0010538"],
            "900В6": ["900B6"],
            "045M6": ["045M6"],
            "62-77E6, 79-81E6": [f"{i}E6" for i in range(62, 82) if i != 78],
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(serial_list(value), expected)

    def test_ambiguity_and_duplicates(self):
        for value in ("02511026-03311026", "900В6 + 900B6", "900В6, 900B6", "1Ж6", "001—07", "62-77E6, ..."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                serial_list(value)
        self.assertEqual(serial_list("02511026-03311026", explicit=True), ["02511026-03311026"])
        self.assertEqual(serial_list("6С 8164047\n345А6", explicit=True), ["6C 8164047", "345A6"])

    def test_profiles_preserve_unconfirmed_fragments(self):
        examples = {
            "ТСП-0193-01-Ex-30(3000)": "ТСП-0193-01-Ex",
            "ТСМ-0193-01-30(3000)": "ТСМ-0193-01",
            "TSP-0193-17М/РЭД-Ex-30(30/3000)": "ТСП-0193-17М-Ex",
            "ТСП-0193-17M/РЭД-Ex-30(30/3000)": "ТСП-0193-17M-Ex",
            "ТСП-0196-17РЭД-Ех-225(5000)": "ТСП-0196-17РЭД-Ех-225(5000)",
            "ТСП-0193/8AL/DS-Ex-110": "ТСП-0193/8AL/DS-Ex-110",
            "ТСП-1393-04AL/DS-100(40)": "ТСП-1393-04AL/DS-100(40)",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(modification_candidate(value), expected)

    def test_metrologist(self):
        for value in ("Еремеева Анна Викторовна", "А.В. Еремеева", "Еремеева А. В."):
            self.assertEqual(metrologist(value), "Еремеева А.В.")
        self.assertEqual(metrologist("Иванова-Петрова Анна Ильинична"), "Иванова-Петрова А.И.")
        for value in ("Еремеева А.", "Еремеева Анна", "", "Еремеева А.В., Петров А.Б."):
            with self.assertRaises(ValueError):
                metrologist(value)


class MaterialTests(unittest.TestCase):
    def test_supplied_workbooks_are_read_only(self):
        before = {path: sha256(path.read_bytes()).hexdigest() for path in (SAMPLE, TEMPLATE, JOURNAL)}
        sample = read_protocol(SAMPLE)
        self.assertEqual(sample.digest, before[SAMPLE])
        self.assertEqual(sample.reader, "rtd-v1")
        self.assertEqual(sample.declared, 25)
        self.assertEqual([r.serial for r in sample.instruments], [f"{i:03d}W6" for i in range(9, 34)])
        self.assertTrue(all(i.elements[0].complete and i.elements[0].status == "СООТВЕТСТВУЕТ"
                            for i in sample.instruments))
        self.assertEqual(sample.proposed_modification, "ТСМ-0193-01")
        self.assertEqual(len(sample.equipment), 8)
        self.assertIsNone(sample.fields["verification_type"].value)
        results = check([sample], {}, [])
        self.assertTrue(all(any("окончательное заключение" in issue for issue in r.issues) for r in results))
        self.assertFalse(any("учебн" in issue.casefold() for r in results for issue in r.issues))
        template = read_protocol(TEMPLATE)
        self.assertEqual(template.reader, sample.reader)
        self.assertEqual(template.instruments, [])
        self.assertTrue(template.issues)
        self.assertTrue(read_protocol(JOURNAL).unsupported)
        self.assertEqual(before, {path: sha256(path.read_bytes()).hexdigest() for path in before})

    def test_schema_and_supplied_xml(self):
        schema = Schema()
        self.assertEqual(schema.hash, SCHEMA_HASH)
        _, messages, details = open_xml(XML_EXAMPLE, schema)
        self.assertEqual(len(messages), 3)
        self.assertEqual(sum("наклейки" in s for s in messages), 2)
        self.assertEqual(sum("наличие средств" in s for s in messages), 1)
        self.assertTrue(details)


def fixture(path, number, serial, *, dual=False, outcome="СООТВЕТСТВУЕТ", complete=True):
    book = Workbook()
    t = book.active
    t.title = "Таблицы"
    r = book.create_sheet("Отчёт")
    d = book.create_sheet("Данные")
    e = book.create_sheet("Эталоны")
    o = book.create_sheet("Оборудование")
    book.create_sheet("Списки")
    from datetime import date
    cells = {
        "B1": "первичной", "D1": number, "A2": "Заводские номера", "B5": 21.3, "B6": 99.3,
        "B7": 30, "B8": 220, "A10": "г/р № 10000-10 Учебное средство № 001",
        "B19": f"№{serial}", "C19": "годны к применению, соответствуют",
        "B20": "01234567890", "B21": "-", "B22": "V", "B23": "Тестова Анна Викторовна",
        "B24": date(2026, 1, 15), "H26": "Схема соединения", "J26": "Кол-во",
        "A27": "ТСП-0193", "B27": "01", "L27": "56560-14", "H27": "2х4" if dual else "4",
        "J27": 1, "A34": "Серийный №",
    }
    for cell, value in cells.items():
        t[cell] = value
    r["A20"] = f"Протокол {number}"
    r["A32"] = "Методика поверки:"
    r["A33"] = "Методика для испытаний XML"
    r["A45"] = "Учебное заключение"
    r["A61"] = "ТСП-0193-01-100"
    e["G1"] = "Поверен до"
    o["E1"] = "Строка для протокола"
    for col, value in {1: "Блок", 2: "Прибор №", 3: "ЧЭ №", 7: "Номер с кодом", 9: "Статус ЧЭ"}.items():
        d.cell(1, col, value)
    for part in range(1, 3 if dual else 2):
        for col, value in enumerate((part, 0, part, serial, "", serial, serial, serial,
                                    outcome if part == 2 or not dual else "СООТВЕТСТВУЕТ"), 1):
            d.cell(part + 1, col, value)
        for row in (33 + part * 2, 34 + part * 2):
            for col in (3, 4, 7, 8, 9, 10):
                if complete or col != 7:
                    t.cell(row, col, 100)
    book.save(path)
    book.close()
    return read_protocol(path)


def equipment():
    return RegistryEntry("г/р № 10000-10 Учебное средство № 001", "10000-10", "001", "Учебное средство",
                         "mis", "", "10000-10", "001", "manufactureNum",
                         "2025-01-01", "2027-01-01", "Условная запись для теста", "Тест")


def approve(storage, p, *, outcome="pass", sticker="01234567890", modification="ТСП-0193-01"):
    evidence, user = "Условное подтверждение для теста", "Тест"
    storage.save_confirmation("modification", modification_key(p), {"modification": modification}, evidence, user)
    storage.save_confirmation("protocol", protocol_key(p),
                              {"sign_cipher": "ВЯ", "calibration": False, "valid_date": "2027-01-14",
                               "one_result": True}, evidence, user)
    for i in p.instruments:
        storage.save_confirmation("instrument", individual_key(p, i.serial),
                                  {"outcome": outcome, "sticker": sticker,
                                   "sign_pass": True, "sign_mi": False,
                                   "reasons": "Отказ второго элемента" if outcome == "fail" else ""},
                                  evidence, user)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = Storage(self.root / "test.sqlite3")
        self.schema = Schema()

    def tearDown(self):
        self.tmp.cleanup()

    def checked(self, *protocols):
        return check(protocols, self.storage.confirmations(), self.storage.entries())

    def test_dual_group_and_negative(self):
        p = fixture(self.root / "two.xlsm", "ТЕСТ-ДВА", "019B6", dual=True, outcome="НЕ СООТВЕТСТВУЕТ")
        self.assertEqual(p.issues, [])
        self.assertEqual(len(p.instruments), 1)
        self.assertEqual([r.index for r in p.instruments[0].elements], [1, 2])
        self.assertEqual(self.checked(p)[0].candidate, "fail")
        self.storage.save_equipment(equipment())
        approve(self.storage, p)
        self.assertTrue(any("противоречит" in q for q in self.checked(p)[0].issues))
        approve(self.storage, p, outcome="fail")
        result = self.checked(p)[0]
        self.assertEqual(result.issues, [])
        payload, _ = format_packets([result], self.schema)[0]
        self.assertIn(b"gost:inapplicable", payload)
        self.assertNotIn(b"gost:stickerNum", payload)
        self.assertNotIn(b"gost:validDate", payload)

    def test_incomplete_measurements_block(self):
        p = fixture(self.root / "incomplete.xlsx", "ТЕСТ-НЕПОЛНЫЙ", "023B6", complete=False)
        self.storage.save_equipment(equipment())
        approve(self.storage, p)
        result = self.checked(p)[0]
        self.assertFalse(result.candidate)
        self.assertTrue(any("неполные" in q for q in result.issues))

    def test_exact_equipment_and_expiration(self):
        p = fixture(self.root / "match.xlsx", "ТЕСТ-РЕЕСТР", "035K6")
        item = p.equipment[0]
        exact = equipment()
        wrong = replace(exact, serial="1", registry_serial="1")
        self.assertIsNone(resolve(item, [wrong], iso_date("2026-01-15"))[0])
        self.assertIsNone(resolve(item, [exact], iso_date("2027-01-02"))[0])
        self.assertIsNone(resolve(item, [exact, exact], iso_date("2026-01-15"))[0])
        self.assertEqual(resolve(item, [exact], iso_date("2026-01-15"))[0], exact)

    def test_export_two_protocols_readback_and_history(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-1", "001B6")
        q = fixture(self.root / "two.xlsx", "ТЕСТ-2", "002B6")
        self.storage.save_equipment(equipment())
        approve(self.storage, p)
        approve(self.storage, q, sticker="01234567891")
        results = self.checked(p, q)
        self.assertEqual([r.issues for r in results], [[], []])
        big_packets = format_packets(results, self.schema)
        split_limit = max(len(format_packets([r], self.schema)[0][0]) for r in results)
        split = format_packets(results, self.schema, split_limit)
        self.assertEqual([len(items) for _, items in split], [1, 1])
        self.assertTrue(all(len(payload) <= split_limit for payload, _ in split))
        paths = export(results, self.schema, self.root, self.storage, [], "Тест")
        self.assertEqual(len(paths), 1)
        root, issues, _ = open_xml(paths[0], self.schema)
        self.assertFalse(issues)
        self.assertEqual([signature(r) for r in root], [item[1] for item in big_packets[0][1]])
        self.assertEqual(root[0].findtext(f"{{{NS}}}metrologist"), "Тестова А.В.")
        self.assertEqual(self.storage.seen([r.identity for r in results]), {r.identity for r in results})
        history = self.storage.history()[0]
        self.assertEqual(history["status"], "Сформирован")
        self.assertEqual(set(self.storage.export_context(history["id"]).values()), {"ТЕСТ-1", "ТЕСТ-2"})
        self.storage.change_status(history["id"], "Загружен", "Учебный сценарий", "Тест")
        self.assertEqual(self.storage.history()[0]["status"], "Загружен")
        backup = self.root / "backup.sqlite3"
        self.storage.backup(backup)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("select count(*) from export_items").fetchone()[0], 2)

    def test_public_lookup_checks_both_exact_identifiers(self):
        response = {"result": {"items": [
            {"vri_id": "test-001", "mit_number": "10000-10", "mi_number": "001", "applicability": True},
            {"vri_id": "test-002", "mit_number": "10000-10", "mi_number": "1"},
            {"vri_id": "test-003", "mit_number": "10001-10", "mi_number": "001"},
        ]}}
        self.assertEqual([x.record_id for x in exact_records(response, "10000-10", "001")], ["test-001"])

    def test_positive_without_sticker_or_registry_and_wrong_fio(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-НАКЛЕЙКА", "034C6")
        approve(self.storage, p, sticker="")
        p.fields["metrologist"] = Datum("Тестова Анна", "Таблицы!B23")
        results = self.checked(p)
        issues = " ".join(results[0].issues)
        self.assertIn("наклейки", issues)
        self.assertIn("ФИФ ОЕИ", issues)
        self.assertIn("поверителя", issues)
        with self.assertRaises(ValueError):
            format_packets(results, self.schema)

    def test_xml_layer_rejects_reversed_metrologist(self):
        p = fixture(self.root / "ready.xlsx", "ТЕСТ-ФИО", "034C6")
        self.storage.save_equipment(equipment())
        approve(self.storage, p)
        results = self.checked(p)
        self.assertFalse(results[0].issues)
        results[0].fields["metrologist"] = Datum("А.В. Тестова", "Таблицы!B23")
        with self.assertRaisesRegex(ValueError, "Фамилия И.О."):
            format_packets(results, self.schema)

    def test_duplicate_identity_is_blocked(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-А", "034C6")
        q = fixture(self.root / "two.xlsx", "ТЕСТ-Б", "034С6")
        self.storage.save_equipment(equipment())
        approve(self.storage, p)
        approve(self.storage, q, sticker="01234567891")
        results = self.checked(p, q)
        self.assertTrue(all(any("Повтор результата" in i for i in r.issues) for r in results))
        with self.assertRaises(ValueError):
            format_packets(results, self.schema)

    def test_untrusted_xml_dtd_not_read(self):
        path = self.root / "dtd.xml"
        path.write_text("<!DOCTYPE foo><foo/>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DTD"):
            open_xml(path, self.schema)

    def test_disk_audit_failure_rolls_back_export(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-ОТКАТ", "034C6")
        self.storage.save_equipment(equipment())
        approve(self.storage, p)
        results = self.checked(p)

        class NoAudit:
            def record_export(self, *_):
                raise OSError("disk")

        with self.assertRaises(OSError):
            export(results, self.schema, self.root, NoAudit(), [], "Тест")
        self.assertFalse(list(self.root.glob("Аршин_*")))
        self.assertFalse(list(self.root.glob(".arshin*")))


if __name__ == "__main__":
    unittest.main()
