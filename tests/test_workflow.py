from contextlib import closing
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from posixpath import normpath
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from lxml import etree
from openpyxl import Workbook

from arshin_generator.arshin import NS, SCHEMA_HASH, Schema, export, format_packets, open_xml, signature, split_paths
from arshin_generator.confirmations import get_confirmation, individual_key, legacy_key, protocol_key, with_guards
from arshin_generator.dates import expiry_before_anniversary
from arshin_generator.domain import Datum, RegistryEntry
from arshin_generator.normalization import modification_candidate, metrologist, serial_list
from arshin_generator.public_registry import exact_records
from arshin_generator.readers import read_protocol
from arshin_generator.readers.rtd import confirmed_rtd_modification
from arshin_generator.registry import iso_date, resolve
from arshin_generator.storage import Storage
from arshin_generator.validation import INVALID_STICKER, check, empty_protocol_issues

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "Файлы для задания"
SAMPLE = MATERIALS / "П-Т1-з2-1260-26.xlsm"
INCOMPLETE = MATERIALS / "П-Т1-з2-1261-26.xlsm"
TEMPLATE = MATERIALS / "П-Т1-з2-!!!!!-26.xltm"
JOURNAL = MATERIALS / "Журнал.xlsx"
XML_EXAMPLE = MATERIALS / "arshin_export_example.xml"
REFERENCE_SCHEMA = ROOT / "arshin_generator" / "schemas" / "import.2022-12-21.xsd"
SAMPLE_METHOD = "ГОСТ 8.461-2009 Термопреобразователи сопротивления из платины, меди и никеля. Методика поверки"


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
        self.assertEqual(confirmed_rtd_modification("56560-14", "ТСП-0193", "17М/РЭД", "Ex"), "ТСП-0193-17М-Ex")
        self.assertFalse(confirmed_rtd_modification("56560-14", "ТСП-0193", "17M/РЭД", "Ex"))

    def test_metrologist(self):
        for value in ("Еремеева Анна Викторовна", "А.В. Еремеева", "Еремеева А. В."):
            self.assertEqual(metrologist(value), "Еремеева А.В.")
        self.assertEqual(metrologist("Иванова-Петрова Анна Ильинична"), "Иванова-Петрова А.И.")
        for value in ("Еремеева А.", "Еремеева Анна", "", "Еремеева А.В., Петров А.Б."):
            with self.assertRaises(ValueError):
                metrologist(value)


class MaterialTests(unittest.TestCase):
    def test_supplied_workbooks_are_read_only(self):
        before = {path: sha256(path.read_bytes()).hexdigest() for path in (SAMPLE, INCOMPLETE, TEMPLATE, JOURNAL)}
        sample = read_protocol(SAMPLE)
        self.assertEqual(sample.digest, before[SAMPLE])
        self.assertEqual(sample.reader, "rtd-v1")
        self.assertEqual(sample.declared, 25)
        self.assertEqual([r.serial for r in sample.instruments], [f"{i:03d}W6" for i in range(9, 34)])
        self.assertTrue(all(i.elements[0].complete and i.elements[0].status == "СООТВЕТСТВУЕТ"
                            for i in sample.instruments))
        self.assertEqual(sample.proposed_modification, "ТСМ-0193-01")
        self.assertEqual([(e.serial, e.registration or e.type_number) for e in sample.equipment], [
            ("83046", "4542-74"), ("3342", "32777.06.2Р.01504903"), ("3342", "32777.06.2Р.01504902"),
            ("231024843", "58550-14"), ("10253831", "19973.06.4Р.01149311"),
            ("21-5558L", "80030.20.3Р.01538249"), ("21-5558L", "80030.20.3Р.01538250"),
        ])
        results = check([sample], {}, [])
        self.assertTrue(all(r.value("outcome") == "pass" for r in results))
        for key, value in {
            "modification": "ТСМ-0193-01", "sign_pass": True, "sign_mi": False,
            "valid_date": "2030-11-21",
            "owner": 'ООО "ЧТП"', "metrologist": "Еремеева А.В.", "verification_date": "2026-11-22",
            "temperature": "21,6 °C", "pressure": "99,83 кПа", "humidity": "30 %", "other": "220 В",
            "type_number": "56560-14", "method": SAMPLE_METHOD,
        }.items():
            with self.subTest(field=key):
                self.assertEqual([r.value(key) for r in results], [value] * 25)
        self.assertTrue(all(not r.value("sticker") for r in results))
        self.assertFalse(any("учебн" in issue.casefold() for r in results for issue in r.issues))
        template = read_protocol(TEMPLATE)
        self.assertEqual(template.reader, sample.reader)
        self.assertEqual(template.instruments, [])
        self.assertTrue(template.issues)
        self.assertIn("ячейку B5", " ".join(empty_protocol_issues(template, {}, {})))
        incomplete = read_protocol(INCOMPLETE)
        self.assertEqual(len(incomplete.instruments), 25)
        self.assertEqual(sum(len(item.elements) for item in incomplete.instruments), 50)
        records = check([incomplete], {}, [], check_registry=False)
        self.assertTrue(all(result.incomplete_measurements for result in records))
        for cell in ("B1", "B5", "B6", "B7"):
            self.assertIn(f"ячейку {cell}", " ".join(records[0].blockers))
        self.assertTrue(read_protocol(JOURNAL).unsupported)
        self.assertEqual(before, {path: sha256(path.read_bytes()).hexdigest() for path in before})

    def test_schema_and_supplied_xml(self):
        schema = Schema()
        self.assertEqual(schema.hash, SCHEMA_HASH)
        self.assertEqual(schema.path.read_bytes(), (MATERIALS / "import.2020-06-19.xsd").read_bytes())
        _, messages, details = open_xml(XML_EXAMPLE, schema)
        self.assertEqual(len(messages), 1)
        self.assertIn("наличие средств", messages[0])
        self.assertTrue(details)
        reference = Schema(REFERENCE_SCHEMA)
        self.assertEqual(reference.hash, "71843c9544546ce2a319f14f8fd645c6d18c080ad73007070815e2a33e2c5911")
        with self.assertRaisesRegex(ValueError, "19.06.2020"):
            format_packets([], reference)


def fixture(path, number, serial, *, dual=False, outcome="СООТВЕТСТВУЕТ", complete=True,
            count=1, print_limit=None, sticker="01234567890", owner="Синтетический владелец", interval=4,
            method="Методика для испытаний XML", day=date(2026, 1, 15), kind="первичной",
            temperature=21.3, execution="01", ex="", inconsistent=None, positive_report=None):
    book = Workbook()
    t = book.active
    t.title = "Таблицы"
    r = book.create_sheet("Отчёт")
    d = book.create_sheet("Данные")
    e = book.create_sheet("Эталоны")
    o = book.create_sheet("Оборудование")
    book.create_sheet("Списки")
    fail = outcome == "НЕ СООТВЕТСТВУЕТ"
    positive = not fail if positive_report is None else positive_report
    conclusion = "годны" if positive else "не годны"
    cells = {
        "B1": kind, "D1": number, "A2": "Заводские номера", "B5": temperature, "B6": 99.3,
        "B7": 30, "B8": 220, "A10": "г/р № 10000-10 Учебное средство № 001",
        "B19": f"№{serial}", "C19": f"{conclusion} к применению",
        "B20": sticker, "B21": "-", "B22": "V", "B23": "Тестова Анна Викторовна",
        "B24": day, "H26": "Схема соединения", "J26": "Кол-во",
        "A27": "ТСП-0193", "B27": execution, "C27": ex, "L27": "56560-14", "H27": "2х4" if dual else "4",
        "J27": count, "K27": interval, "A34": "Серийный №",
    }
    for cell, value in cells.items():
        t[cell] = value
    r["A20"] = f"Протокол {number}"
    r["J24"] = owner
    r["A32"] = "Методика поверки:"
    r["A33"] = method
    r["A45"] = f"Заключение: СИ {conclusion} к применению."
    if fail and not positive:
        t["F20"] = "Отклонение за пределами допуска"
    r["A61"] = "ТСП-0193-01-100"
    e["G1"] = "Поверен до"
    o["E1"] = "Строка для протокола"
    o.append(["10000-10", "Учебное средство", "001", "г/р", t["A10"].value])
    for col, value in {1: "Блок", 2: "Прибор №", 3: "ЧЭ №", 7: "Номер с кодом", 9: "Статус ЧЭ"}.items():
        d.cell(1, col, value)
    if print_limit is not None:
        d["L1"] = print_limit
    names = [serial] if count == 1 else [f"{i + 1:03d}B6" for i in range(count)]
    t["B19"] = "№" + ", ".join(names)
    per_device = 2 if dual else 1
    for instrument, name in enumerate(names):
        for part in range(1, per_device + 1):
            block = instrument * per_device + part
            for col, value in enumerate((block, instrument, part, name, "", name, name, name,
                                         outcome if part == per_device else "СООТВЕТСТВУЕТ"), 1):
                d.cell(block + 1, col, value)
            for row in (33 + block * 2, 34 + block * 2):
                for col in (3, 4, 7, 8, 9, 10):
                    if complete or col != 7:
                        outside = (fail and part == per_device) != (inconsistent == (instrument, part))
                        t.cell(row, col, 101 if col == 8 and outside else 100)
    book.save(path)
    book.close()
    return read_protocol(path)


def sample_copy_with_kind(path, kind):
    """Make an isolated test copy, retaining the original formula values and macros."""
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rels_uri = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with ZipFile(SAMPLE) as original, ZipFile(path, "w") as copied:
        workbook = etree.fromstring(original.read("xl/workbook.xml"))
        sheet = workbook.find(f".//{{{namespace}}}sheet[@name='Таблицы']")
        rel_id = sheet.get(f"{{{rels_uri}}}id")
        rels = etree.fromstring(original.read("xl/_rels/workbook.xml.rels"))
        target = next(rel.get("Target") for rel in rels if rel.get("Id") == rel_id)
        name = target.lstrip("/") if target.startswith("/") else normpath(f"xl/{target}")
        for item in original.infolist():
            contents = original.read(item)
            if item.filename == name:
                sheet = etree.fromstring(contents)
                row = sheet.find(f".//{{{namespace}}}row[@r='1']")
                cell = row.find(f"{{{namespace}}}c[@r='B1']")
                if cell is None:
                    cell = etree.Element(f"{{{namespace}}}c", r="B1")
                    row.insert(1, cell)
                for child in list(cell):
                    cell.remove(child)
                cell.set("t", "inlineStr")
                etree.SubElement(etree.SubElement(cell, f"{{{namespace}}}is"), f"{{{namespace}}}t").text = kind
                contents = etree.tostring(sheet, encoding="utf-8")
            copied.writestr(item, contents)
    return path


def equipment():
    return RegistryEntry("г/р № 10000-10 Учебное средство № 001", "10000-10", "001", "Учебное средство",
                         "mis", "", "10000-10", "001", "manufactureNum",
                         "2025-01-01", "2027-01-01", "Условная запись для теста", "Тест")


def seed_lab(storage):
    storage.save_laboratory({"sign_cipher": "ВЯ", "calibration": False}, "Тест")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = Storage(self.root / "test.sqlite3")
        self.schema = Schema()

    def tearDown(self):
        self.tmp.cleanup()

    def checked(self, *protocols):
        return check(protocols, self.storage.confirmations(), self.storage.entries(), laboratory=self.storage.laboratory())

    def test_batch_corrections_are_atomic(self):
        rows = [("protocol", "A", {"sign_cipher": "ТСТ"}, "Тест", "Тест"),
                ("instrument", "B", {"sign_mi": False}, "Тест", "Тест")]
        with patch.object(self.storage, "_audit", side_effect=[None, sqlite3.OperationalError("disk")]):
            with self.assertRaises(sqlite3.OperationalError):
                self.storage.save_confirmations(rows)
        self.assertEqual(self.storage.confirmations(), {})
        self.storage.save_confirmations(rows)
        self.assertEqual(len(self.storage.confirmations()), 2)

    def test_lab_defaults_leave_excel_kind_authoritative_and_ignore_legacy_expiry(self):
        day = date(2026, 11, 23)
        protocol = fixture(self.root / "source.xlsx", "ТЕСТ-СРОК", "001B6", day=day)
        seed_lab(self.storage)
        settings = {**self.storage.laboratory(), "verification_type": "2",
                    "expiry_rules": {"56560-14": {"years": 2}}}
        self.storage.save_laboratory(settings, "Тест")
        self.storage.save_confirmation("protocol", protocol_key(protocol),
            {"valid_date": "2027-01-14", "verification_type": "2"}, "Старые сведения", "Тест")
        self.storage.save_confirmation("expiry_exception", protocol_key(protocol),
            {"valid_date": "2038-11-23"}, "Старое исключение", "Тест")
        self.storage.save_confirmation("instrument", individual_key(protocol, "001B6"),
            {"valid_date": "2028-11-23", "outcome": "fail"}, "Старые сведения", "Тест")
        self.storage.save_equipment(equipment())
        result = self.checked(protocol)[0]
        self.assertEqual(result.value("verification_type"), "1")
        self.assertIs(result.value("calibration"), False)
        self.assertEqual(result.value("outcome"), "pass")
        self.assertEqual(result.value("valid_date"), "2030-11-22")
        self.assertIn("Таблицы!B24 и K27", result.fields["valid_date"].source)
        self.assertFalse(result.blockers)

        protocol.fields["verification_type"] = Datum(None, "Таблицы!B1")
        result = self.checked(protocol)[0]
        self.assertIsNone(result.value("verification_type"))
        self.assertIn("ячейку B1", " ".join(result.blockers))

    def test_modification_comes_from_structured_excel_values(self):
        known = fixture(self.root / "known.xlsx", "ТЕСТ-ИСПОЛНЕНИЕ", "011B6", execution="01Б", ex="Ex-")
        self.assertEqual(known.fields["modification"].value, "ТСП-0193-01Б-Ex")
        unknown = fixture(self.root / "unknown.xlsx", "ТЕСТ-АЛЬТЕРНАТИВА", "012B6", execution="01Заказ")
        self.storage.save_confirmation("protocol", protocol_key(unknown),
            {"modification": "ТСП-0193-Произвольное"}, "Старое значение", "Тест")
        record = self.checked(unknown)[0]
        self.assertFalse(record.value("modification"))
        self.assertIn("Не удалось определить модификацию по данным протокола", " ".join(record.blockers))
        self.assertIn("A27:C27", " ".join(record.blockers))

    def test_full_batch_without_stickers_roundtrips_with_method_and_equipment_identity(self):
        protocol = fixture(self.root / "batch.xlsx", "ТЕСТ-ПАКЕТ", "001B6", count=25, dual=True, sticker="",
                           method="  ГОСТ 8.461-2009\n Термопреобразователи   СИ  ")
        self.storage.save_equipment(replace(equipment(), identity_kind="inventoryNum"))
        self.storage.save_confirmation("protocol", protocol_key(protocol), {
            "sign_cipher": "ТСТ", "calibration": False, "valid_date": "2027-01-14",
        }, "Синтетический срок для проверки", "Тест")
        results = self.checked(protocol)
        self.assertTrue(all(not result.blockers for result in results))
        [file] = export(format_packets(results, self.schema), self.schema, [self.root / "Контрольный.xml"],
                        self.storage, "Тест")
        root, issues, _ = open_xml(file, self.schema)
        ns = {"g": NS}
        self.assertFalse(issues)
        self.assertEqual(len(root), 25)
        self.assertEqual([r.findtext(f".//{{{NS}}}manufactureNum") for r in root],
                         [f"{i:03d}B6" for i in range(1, 26)])
        self.assertEqual(root.xpath("g:result/g:docTitle/text()", namespaces=ns),
                         ["ГОСТ 8.461-2009 Термопреобразователи СИ"] * 25)
        self.assertEqual(root.xpath("g:result/g:means/g:mis/g:mi/g:inventoryNum/text()", namespaces=ns), ["001"] * 25)
        self.assertFalse(root.xpath("g:result/g:means/g:mis/g:mi/g:manufactureNum", namespaces=ns))
        self.assertFalse(root.xpath("g:result/g:applicable/g:stickerNum", namespaces=ns))

    def test_optional_sticker_uses_original_schema_format(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-НАКЛЕЙКА", "034C6", sticker="00А-12")
        self.storage.save_equipment(equipment())
        self.storage.save_confirmation("protocol", protocol_key(p), {
            "sign_cipher": "ТСТ", "calibration": False, "valid_date": "2027-01-14",
        }, "Тест", "Тест")
        payload, _ = format_packets(self.checked(p), self.schema)[0]
        root, issues, _ = self.schema.inspect(payload)
        self.assertFalse(issues)
        self.assertEqual(root.findtext(f".//{{{NS}}}stickerNum"), "00А-12")
        p.fields["sticker"] = Datum("A" * 129, "Таблицы!B20")
        self.assertIn(INVALID_STICKER, " ".join(self.checked(p)[0].blockers))

    def test_batch_sticker_requires_a_specific_instrument(self):
        p = fixture(self.root / "batch.xlsx", "ТЕСТ-НАКЛЕЙКА", "001B6", sticker="00А-12", count=2)
        self.storage.save_equipment(equipment())
        self.storage.save_confirmation("protocol", protocol_key(p), {
            "sign_cipher": "ТСТ", "calibration": False, "valid_date": "2027-01-14",
        }, "Тест", "Тест")
        self.assertTrue(all("Один номер наклейки указан на партию" in " ".join(r.blockers) for r in self.checked(p)))
        self.storage.save_confirmation("instrument", individual_key(p, p.instruments[1].serial),
                                       {"sticker": "00А-12"}, "Документ", "Тест")
        payload, _ = format_packets(self.checked(p), self.schema)[0]
        root, issues, _ = self.schema.inspect(payload)
        self.assertFalse(issues)
        self.assertIsNone(root[0].find(f".//{{{NS}}}stickerNum"))
        self.assertEqual(root[1].findtext(f".//{{{NS}}}stickerNum"), "00А-12")

    def test_dual_group_and_negative(self):
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        p = fixture(self.root / "two.xlsm", "ТЕСТ-ДВА", "019B6", dual=True, outcome="НЕ СООТВЕТСТВУЕТ")
        self.assertEqual(len(p.instruments), 1)
        self.assertEqual([r.index for r in p.instruments[0].elements], [1, 2])
        result = self.checked(p)[0]
        self.assertEqual(result.value("outcome"), "fail")
        self.assertFalse(result.blockers)
        payload, _ = format_packets([result], self.schema)[0]
        self.assertIn(b"gost:inapplicable", payload)
        self.assertNotIn(b"gost:stickerNum", payload)
        self.assertNotIn(b"gost:validDate", payload)

        opposite = fixture(self.root / "opposite.xlsx", "ТЕСТ-ПРОТИВОРЕЧИЕ", "019B6", dual=True,
                           outcome="НЕ СООТВЕТСТВУЕТ", positive_report=True)
        self.assertIn("заключение расходится с измерениями", " ".join(self.checked(opposite)[0].blockers))
        stale = fixture(self.root / "stale.xlsx", "ТЕСТ-КЭШ-ФОРМУЛЫ", "019B6", dual=True, inconsistent=(0, 2))
        self.assertIn("расходится с допусками", " ".join(self.checked(stale)[0].blockers))

        positive = fixture(self.root / "positive.xlsx", "ТЕСТ-ДВА-ПРИГОДЕН", "019B6", dual=True, sticker="")
        result = self.checked(positive)[0]
        self.assertFalse(result.blockers)
        root, issues, _ = self.schema.inspect(format_packets([result], self.schema)[0][0])
        self.assertFalse(issues)
        self.assertEqual(len(root), 1)
        self.assertEqual(root.findtext(f".//{{{NS}}}manufactureNum"), "019B6")

    def test_incomplete_measurements_block(self):
        p = fixture(self.root / "incomplete.xlsx", "ТЕСТ-НЕПОЛНЫЙ", "023B6", complete=False)
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        result = self.checked(p)[0]
        self.assertFalse(result.value("outcome"))
        self.assertTrue(result.incomplete_measurements)
        self.assertEqual(sum("измерения" in issue for issue in result.issues), 1)

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
        q = fixture(self.root / "two.xlsx", "ТЕСТ-2", "002B6", sticker="01234567891")
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        seed_lab(self.storage)
        results = self.checked(p, q)
        self.assertEqual([r.blockers for r in results], [(), ()])
        big_packets = format_packets(results, self.schema)
        split_limit = max(len(format_packets([r], self.schema)[0][0]) for r in results)
        split = format_packets(results, self.schema, split_limit)
        self.assertEqual([len(items) for _, items in split], [1, 1])
        self.assertTrue(all(len(payload) <= split_limit for payload, _ in split))
        paths = export(big_packets, self.schema, [self.root / "Итоговый.xml"], self.storage, "Тест")
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
        seed_lab(self.storage)
        p.fields["metrologist"] = Datum("Тестова Анна", "Таблицы!B23")
        results = self.checked(p)
        issues = " ".join(results[0].blockers)
        self.assertNotIn("наклейки", issues)
        self.assertIn("ФИФ ОЕИ", issues)
        self.assertIn("поверителя", issues)
        with self.assertRaises(ValueError):
            format_packets(results, self.schema)

    def test_xml_layer_rejects_reversed_metrologist(self):
        p = fixture(self.root / "ready.xlsx", "ТЕСТ-ФИО", "034C6")
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        results = self.checked(p)
        self.assertFalse(results[0].blockers)
        results[0].fields["metrologist"] = Datum("А.В. Тестова", "Таблицы!B23")
        with self.assertRaisesRegex(ValueError, "Фамилия И.О."):
            format_packets(results, self.schema)

    def test_duplicate_identity_is_blocked(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-А", "034C6")
        q = fixture(self.root / "two.xlsx", "ТЕСТ-Б", "034С6")
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        seed_lab(self.storage)
        results = self.checked(p, q)
        self.assertTrue(all(any("Повтор результата" in i for i in r.issues) for r in results))
        with self.assertRaises(ValueError):
            format_packets(results, self.schema)

    def test_completed_workbook_exports_without_individual_outcome_confirmation(self):
        p = fixture(self.root / "automatic.xlsx", "ТЕСТ-АВТО", "056B6")
        self.storage.save_equipment(equipment())
        fields = {"sign_cipher": "ВЯ", "calibration": False, "valid_date": "2027-01-14"}
        self.storage.save_confirmation("protocol", protocol_key(p), with_guards(p, fields),
                                       "Протокол", "Тест")
        result = self.checked(p)[0]
        self.assertEqual(result.value("outcome"), "pass")
        self.assertIn("Данные!", result.fields["outcome"].source)
        self.assertFalse(result.blockers)
        self.assertEqual(len(format_packets([result], self.schema)), 1)

    def test_print_limit_warns_and_block_63_is_read(self):
        p = fixture(self.root / "batch.xlsx", "ТЕСТ-ВМЕСТИМОСТЬ", "unused", count=63, print_limit=62)
        self.assertEqual(len(p.instruments), 63)
        self.assertEqual(p.instruments[-1].serial, "063B6")
        self.assertTrue(p.instruments[-1].elements[0].complete)
        self.assertFalse(p.issues)
        self.assertIn("62", p.notices[0])

    def test_confirmation_migrates_and_only_changed_evidence_is_invalidated(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-ПЕРЕНОС", "034C6")
        instrument = p.instruments[0]
        self.storage.save_confirmation("instrument", legacy_key(p, instrument),
                                       {"sticker": "01234567890", "outcome": "pass"}, "Документ", "Тест")
        self.storage.migrate_confirmation(p)
        changed = replace(p, digest="updated-sha")
        fields = get_confirmation(self.storage.confirmations(), "instrument", changed, instrument)[0]
        self.assertEqual(fields, {"outcome": "pass", "sticker": "01234567890"})
        first = instrument.elements[0]
        new_instrument = replace(instrument, elements=(replace(first, status="НЕ СООТВЕТСТВУЕТ"),))
        fields = get_confirmation(self.storage.confirmations(), "instrument", changed, new_instrument)[0]
        self.assertEqual(fields, {"sticker": "01234567890"})
        changed_day = replace(changed, fields={**p.fields, "verification_date": Datum("2026-03-11", "B24")})
        self.assertIsNone(get_confirmation(self.storage.confirmations(), "instrument", changed_day, new_instrument))

    def test_untrusted_xml_dtd_not_read(self):
        path = self.root / "dtd.xml"
        path.write_text("<!DOCTYPE foo><foo/>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DTD"):
            open_xml(path, self.schema)

    def test_disk_audit_failure_rolls_back_export(self):
        p = fixture(self.root / "one.xlsx", "ТЕСТ-ОТКАТ", "034C6")
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        results = self.checked(p)

        class NoAudit:
            def record_export(self, *_):
                raise OSError("disk")

        with self.assertRaises(OSError):
            export(format_packets(results, self.schema), self.schema, [self.root / "Аршин_Тест.xml"], NoAudit(), "Тест")
        self.assertFalse(list(self.root.glob("Аршин_*")))
        self.assertFalse(list(self.root.glob(".arshin*")))

    def test_existing_file_requires_permission_and_is_restored_on_audit_failure(self):
        protocol = fixture(self.root / "one.xlsx", "ТЕСТ-ЗАМЕНА", "034C6")
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        packets = format_packets(self.checked(protocol), self.schema)
        path = self.root / "Выбранный файл.xml"
        path.write_bytes(b"previous contents")
        with self.assertRaises(FileExistsError):
            export(packets, self.schema, [path], self.storage, "Тест")
        self.assertEqual(path.read_bytes(), b"previous contents")
        with patch.object(self.storage, "record_export", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                export(packets, self.schema, [path], self.storage, "Тест", overwrite=path.stat())
        self.assertEqual(path.read_bytes(), b"previous contents")
        self.assertFalse(list(self.root.glob(".arshin*")))
        export(packets, self.schema, [path], self.storage, "Тест", overwrite=path.stat())
        self.assertFalse(open_xml(path, self.schema)[1])

    def test_split_collision_rolls_back_parts_already_written(self):
        first = fixture(self.root / "first.xlsx", "ТЕСТ-1", "034C6")
        second = fixture(self.root / "second.xlsx", "ТЕСТ-2", "035C6", sticker="01234567891")
        self.storage.save_equipment(equipment())
        seed_lab(self.storage)
        seed_lab(self.storage)
        results = self.checked(first, second)
        packets = format_packets(results, self.schema, max(len(format_packets([r], self.schema)[0][0]) for r in results))
        paths = split_paths(self.root, "Аршин", len(packets))
        paths[1].write_bytes(b"new external file")
        with self.assertRaises(FileExistsError):
            export(packets, self.schema, paths, self.storage, "Тест")
        self.assertFalse(paths[0].exists())
        self.assertEqual(paths[1].read_bytes(), b"new external file")
        self.assertEqual(self.storage.history(), [])
        self.assertTrue(all(not path.exists() for path in split_paths(self.root, "Аршин", len(packets))))

    def test_expiry_calendar_and_invalid_rtd_inputs(self):
        self.assertEqual(expiry_before_anniversary(date(2026, 11, 22), 4), date(2030, 11, 21))
        self.assertEqual(expiry_before_anniversary(date(2026, 11, 23), 4), date(2030, 11, 22))
        self.assertEqual(expiry_before_anniversary(date(2024, 2, 29), 4), date(2028, 2, 28))
        with self.assertRaisesRegex(ValueError, "29 февраля"):
            expiry_before_anniversary(date(2024, 2, 29), 1)

        for given, interval, expected in ((None, 4, "ячейку B24"), (date(2026, 11, 22), None, "ячейку K27"),
                                           (date(2026, 11, 22), 1.5, "ячейку K27"), (date(2024, 2, 29), 1, "29 февраля")):
            with self.subTest(given=given, interval=interval):
                protocol = fixture(self.root / "expiry.xlsx", "ТЕСТ-СРОК", "001B6", day=given, interval=interval)
                self.assertIn(expected, " ".join(self.checked(protocol)[0].blockers))

    def test_two_elements_require_same_serial_and_both_blocks(self):
        from openpyxl import load_workbook
        path = self.root / "channels.xlsx"
        for cell, value, issue in (("G3", "020B6", "расходятся заводские номера"),
                                   ("C3", 1, "повторный чувствительный элемент")):
            with self.subTest(cell=cell):
                fixture(path, "ТЕСТ-ЧЭ", "019B6", dual=True)
                book = load_workbook(path)
                book["Данные"][cell] = value
                if cell == "G3":
                    book["Данные"]["F3"] = value
                book.save(path)
                book.close()
                self.assertIn(issue, " ".join(self.checked(read_protocol(path))[0].blockers))


if __name__ == "__main__":
    unittest.main()
