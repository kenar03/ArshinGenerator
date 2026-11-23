from dataclasses import replace
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from arshin_generator.arshin import NS, Schema, format_packets
from arshin_generator.domain import Equipment
from arshin_generator.public_registry import PublicClient, PublicDataError
from arshin_generator.registry import lookup_equipment, lookup_key, resolve
from arshin_generator.storage import Storage
from arshin_generator.validation import check

from .test_workflow import approve, fixture


DAY = date(2026, 11, 23)


def etalon(**changes):
    source = Equipment("Отображаемая строка", "Оборудование!A4:D4", "32777-06",
                       "003342", "Термометр ПТСВ-1-2", category="mieta", type_number="32777-06")
    return replace(source, **changes)


def mieta(identifier, number, scheme, *, serial="003342", expiry="13.04.2027"):
    row = {"rmieta_id": identifier, "number": number, "mitype_num": "32777-06", "factory_num": serial,
           "modification": "ПТСВ-1-2", "applicability": True}
    detail = {**row, "npenumber": scheme, "cresults": [
        {"vri_id": "1-" + identifier, "verification_date": "14.04.2026", "valid_date": expiry,
         "applicability": True}
    ]}
    return row, detail


class FixtureClient:
    def __init__(self, standards=(), verifications=(), *, unavailable=False):
        self.standards = standards
        self.verifications = verifications
        self.unavailable = unavailable
        self.requests = []

    def items(self, resource, **filters):
        self.requests.append((resource, filters))
        if self.unavailable:
            raise PublicDataError("Реестр недоступен.")
        if resource == "mit":
            return [{"number": filters["number"], "mit_uuid": "test", "notation": "ПТСВ"}]
        if resource == "mieta":
            return [record for record, _ in self.standards if all(record[k] == v for k, v in filters.items())]
        if resource == "vri":
            return self.verifications
        raise AssertionError(resource)

    def detail(self, resource, record_id):
        self.requests.append((resource, record_id))
        self.assert_mieta(resource)
        return next(detail for row, detail in self.standards if row["rmieta_id"] == record_id)

    @staticmethod
    def assert_mieta(resource):
        if resource != "mieta":
            raise AssertionError(resource)


class RegistryLookupTests(unittest.TestCase):
    def setUp(self):
        self.first = mieta("100001", "32777.06.2Р.01000001", "ГПС 1")
        self.second = mieta("100002", "32777.06.2Р.01000002", "ГПС 2")
        self.expired = mieta("100003", "32777.06.2Р.01000003", "ГПС 3", expiry="20.11.2026")
        self.client = FixtureClient((self.first, self.second, self.expired))

    def test_multiple_etalon_cards_require_a_specific_saved_choice(self):
        item = etalon()
        found = lookup_equipment(item, DAY, self.client)
        self.assertEqual(self.client.requests[0], ("mieta", {"mitype_num": "32777-06", "factory_num": "003342"}))
        self.assertEqual(found.state, "choice")
        self.assertEqual(sum(c.eligible for c in found.candidates), 2)
        self.assertIsNone(resolve(item, [], DAY, found)[0])
        selected = replace(found.candidates[1].entry, confirmer="Поверитель", evidence="Использована схема 2",
                           valid_until="2027-01-01")
        edited = replace(item, description="Изменённая подпись")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "database.sqlite3"
            Storage(path).save_equipment_choice(selected)
            actual = resolve(edited, Storage(path).entries(), DAY, found)[0]
        self.assertEqual((actual.identifier, actual.valid_until), ("32777.06.2Р.01000002", "2027-04-13"))
        conflict = lookup_equipment(item, date(2028, 1, 1), self.client)
        self.assertEqual(conflict.state, "conflict")
        self.assertIsNone(resolve(item, [selected], DAY, conflict)[0])

    def test_complete_registration_disambiguates_but_latin_spelling_requires_review(self):
        number = self.first[0]["number"]
        item = etalon(source_id=number, type_number="", registration=number)
        exact = lookup_equipment(item, DAY, self.client)
        self.assertEqual(exact.state, "found")
        self.assertEqual(exact.candidates[0].entry.identifier, number)
        changed = number.replace("Р", "P")
        variant = lookup_equipment(replace(item, source_id=changed, registration=changed), DAY, self.client)
        self.assertEqual(variant.state, "choice")
        self.assertEqual(variant.candidates[0].entry.identifier, number)

    def test_two_explicit_registrations_for_same_unit_survive_xml_export(self):
        numbers = [self.first[0]["number"], self.second[0]["number"]]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            p = fixture(root / "source.xlsx", "ТЕСТ-ДВЕ-СХЕМЫ", "012B6")
            p.equipment = [etalon(source_id=number, type_number="", registration=number) for number in numbers]
            day = date.fromisoformat(p.fields["verification_date"].value)
            fresh = FixtureClient(tuple(
                (record, {**detail, "cresults": [
                    {**detail["cresults"][0], "verification_date": "01.01.2026"}]})
                for record, detail in (self.first, self.second)
            ))
            lookups = {lookup_key(item, day.isoformat()): lookup_equipment(item, day, fresh)
                       for item in p.equipment}
            storage = Storage(root / "database.sqlite3")
            approve(storage, p)
            results = check([p], storage.confirmations(), [], lookups)
            self.assertFalse(results[0].blockers)
            schema = Schema()
            payload = format_packets(results, schema)[0][0]
            root, issues, _ = schema.inspect(payload)
            self.assertFalse(issues)
            self.assertEqual([node.text for node in root.findall(f".//{{{NS}}}mieta/{{{NS}}}number")], numbers)

    def test_full_identifier_does_not_override_serial_or_model(self):
        number = self.first[0]["number"]
        for item in (etalon(registration=number, serial="3342"),
                     etalon(registration=number, name="Термометр ПТСВ-1-1")):
            found = lookup_equipment(item, DAY, self.client)
            self.assertEqual(found.state, "conflict")
            self.assertFalse(found.candidates[0].eligible)

    def test_vri_later_negative_blocks_an_older_positive_and_offline_has_fallback(self):
        item = replace(etalon(), category="mis", name="Термометр ПТСВ-1-2")
        row = {"vri_id": "1-111", "mit_number": "32777-06", "mi_number": "003342", "mi_modification": "ПТСВ-1-2",
               "verification_date": "14.04.2026", "valid_date": "13.04.2027", "applicability": True}
        client = FixtureClient(verifications=[row])
        good = lookup_equipment(item, DAY, client)
        self.assertEqual(good.state, "found")
        self.assertIn(("vri", {"mit_number": "32777-06", "mi_number": "003342",
                               "verification_date_end": "2026-11-23"}), client.requests)
        saved = good.candidates[0].entry
        negative = {**row, "vri_id": "1-222", "verification_date": "21.11.2026", "applicability": False}
        conflict = lookup_equipment(item, DAY, FixtureClient(verifications=[row, negative]))
        self.assertEqual(conflict.state, "conflict")
        self.assertIsNone(resolve(item, [saved], DAY, conflict)[0])
        offline = lookup_equipment(item, DAY, FixtureClient(unavailable=True))
        self.assertIs(resolve(item, [saved], DAY, offline)[0], saved)

    def test_pagination_requires_complete_pages(self):
        class Pages(PublicClient):
            def get(self, resource, filters):
                start = filters["start"]
                return {"count": 2, "start": start, "items": [{"number": str(start)}]}

        self.assertEqual(len(Pages().items("mieta", number="known")), 2)

        class Broken(Pages):
            def get(self, resource, filters):
                return super().get(resource, {"start": 0})

        with self.assertRaises(PublicDataError):
            Broken().items("mieta", number="known")

    def test_temporary_server_response_is_retried_once(self):
        error = HTTPError("https://fgis.gost.ru", 503, "not available", {}, None)
        responses = iter([error, BytesIO(b'{"result": {"items": [], "count": 0, "start": 0}}')])

        def opener(request, timeout):
            response = next(responses)
            if isinstance(response, HTTPError):
                raise response
            return response

        with patch("arshin_generator.public_registry.sleep") as delay:
            self.assertEqual(PublicClient(opener).items("mit", number="32777-06"), [])
        delay.assert_called_once()


if __name__ == "__main__":
    unittest.main()
