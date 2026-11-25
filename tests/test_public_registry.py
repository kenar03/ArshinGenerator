from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from arshin_generator.arshin import NS, Schema, format_packets
from arshin_generator.domain import Equipment
from arshin_generator.public_registry import PublicClient, PublicDataError, official_type_title
from arshin_generator.registry import lookup_equipment, lookup_key, resolve, resolve_many
from arshin_generator.storage import Storage
from arshin_generator.validation import check
from arshin_generator.verification import RegistrySession

from .test_workflow import seed_lab, fixture


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


@contextmanager
def registry_http():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            self.server.calls[self.path] += 1
            resource = url.path.removeprefix("/")
            status, result = self.server.response(resource, query)
            body = json.dumps({"result": result}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if status == 429:
                self.send_header("Retry-After", "3600")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    def page(records, filters):
        return {"items": records, "count": len(records), "start": int(filters.get("start", 0))}

    def response(resource, filters):
        if resource == "mit":
            number = filters["number"]
            return 200, page([{"number": number, "mit_uuid": number, "title": f"Тип {number}",
                               "notation": "Учебное средство"}], filters)
        if resource == "vri":
            if server.failure:
                return server.failure, {}
            return 200, page([{"vri_id": "1-111", "mit_number": filters["mit_number"],
                               "mi_number": filters["mi_number"], "mi_modification": "Учебное средство",
                               "verification_date": "2025-01-01", "valid_date": server.valid_until,
                               "applicability": True}], filters)
        if resource == "mieta":
            return 200, page([row for row, _ in server.standards
                             if all(row.get(k) == v for k, v in filters.items() if k not in {"rows", "start"})], filters)
        if resource.startswith(("mieta/", "vri/")):
            path, identifier = resource.split("/", 1)
            return 200, FixtureClient(server.standards).detail(path, identifier)
        return 404, {}

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.calls = Counter()
        server.failure = 0
        server.valid_until = "2027-01-01"
        server.standards = []
        server.response = response
        server.opener = build_opener(ProxyHandler({})).open
        task = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        task.start()
        try:
            with patch("arshin_generator.public_registry.API", f"http://127.0.0.1:{server.server_port}/"):
                yield server
        finally:
            server.shutdown()
            task.join()


class FixtureClient:
    def __init__(self, standards=(), verifications=(), *, unavailable=False):
        self.standards = standards
        self.verifications = verifications
        self.unavailable = unavailable
        self.requests = []

    def items(self, resource, **filters):
        self.requests.append((resource, filters))
        if self.unavailable:
            raise PublicDataError("Реестр недоступен.", kind="connection")
        if resource == "mit":
            return [{"number": filters["number"], "mit_uuid": "test", "notation": "ПТСВ"}]
        if resource == "mieta":
            return [record for record, _ in self.standards if all(record[k] == v for k, v in filters.items())]
        if resource == "vri":
            return self.verifications
        raise AssertionError(resource)

    def detail(self, resource, record_id):
        self.requests.append((resource, record_id))
        if resource == "mieta":
            return next(detail for row, detail in self.standards if row["rmieta_id"] == record_id)
        if resource == "vri":
            detail, proof = next((detail, proof) for _, detail in self.standards
                                 for proof in detail["cresults"] if proof["vri_id"] == record_id)
            return {
                "miInfo": {"etaMI": {"regNumber": detail["number"], "mitypeNumber": detail["mitype_num"],
                                     "manufactureNum": detail["factory_num"], "modification": detail["modification"]}},
                "vriInfo": {"applicable": {}, "vrfDate": proof["verification_date"], "validDate": proof["valid_date"]},
            }
        raise AssertionError(resource)


class RegistryLookupTests(unittest.TestCase):
    def setUp(self):
        self.first = mieta("100001", "32777.06.2Р.01000001", "ГПС 1")
        self.second = mieta("100002", "32777.06.2Р.01000002", "ГПС 2")
        self.expired = mieta("100003", "32777.06.2Р.01000003", "ГПС 3", expiry="20.11.2026")
        self.client = FixtureClient((self.first, self.second, self.expired))

    def test_http_deduplication_changed_date_model_and_expiry(self):
        day = date(2026, 1, 15)
        item = Equipment("Средство", "Книга", "10000-10", "001", "Учебное средство", category="mis",
                         type_number="10000-10")
        ticks = [0]
        numbers = {item.type_number}
        with registry_http() as server:
            session = RegistrySession(ttl=30, clock=lambda: ticks[0], opener=server.opener)

            def batch(equipment, when):
                requests = {lookup_key(unit, when.isoformat()): (unit, when) for unit in equipment}
                session.remember(*session.check(*session.pending(requests, numbers)))
                return session.snapshot(requests, numbers)

            lookup, types = batch([item, item], day)
            self.assertEqual(types[item.type_number].title, "Тип 10000-10")
            self.assertEqual(next(iter(lookup.values())).state, "found")
            self.assertEqual(sum(server.calls.values()), 2)  # MIT is shared by the type and the equipment.
            batch([item], day)
            self.assertEqual(sum(server.calls.values()), 2)

            next_day = date(2026, 1, 16)
            batch([item], next_day)
            self.assertEqual(sum(server.calls.values()), 3)
            changed, _ = batch([replace(item, name="Другая модель")], next_day)
            self.assertEqual(next(iter(changed.values())).state, "conflict")
            self.assertEqual(sum(server.calls.values()), 4)

            ticks[0] = 31
            key = lookup_key(item, day.isoformat())
            self.assertEqual(session.snapshot({key: (item, day)}, numbers), ({}, {}))
            batch([item], day)
            self.assertEqual(sum(server.calls.values()), 6)

    def test_http_429_is_bounded_and_manual_retry_only_checks_failed_keys(self):
        item = Equipment("Средство", "Книга", "10000-10", "001", "Учебное средство", category="mis",
                         type_number="10000-10")
        key = lookup_key(item, DAY.isoformat())
        requests, numbers = {key: (item, DAY)}, {item.type_number}
        with registry_http() as server:
            server.failure = 429
            session = RegistrySession(opener=server.opener)
            session.remember(*session.check(*session.pending(requests, numbers)))
            self.assertEqual(session.snapshot(requests, numbers)[0][key].state, "unavailable")
            self.assertEqual(session.pending(requests, numbers), ({}, set()))
            self.assertEqual(sum(server.calls.values()), 2)
            server.failure = 0
            retry = session.pending(requests, numbers, (requests, numbers))
            self.assertEqual(retry, (requests, set()))
            session.remember(*session.check(*retry))
            self.assertEqual(session.snapshot(requests, numbers)[0][key].state, "found")
            self.assertEqual(sum(server.calls.values()), 3)

    def test_mieta_http_choices_share_exact_mit(self):
        item = etalon()
        key = lookup_key(item, DAY.isoformat())
        requests, numbers = {key: (item, DAY)}, {item.type_number}
        with registry_http() as server:
            server.standards = [self.first, self.second]
            session = RegistrySession(opener=server.opener)
            session.remember(*session.check(*session.pending(requests, numbers)))
            decision = session.snapshot(requests, numbers)[0][key]
            self.assertEqual(decision.state, "choice")
            self.assertEqual(len([candidate for candidate in decision.candidates if candidate.eligible]), 2)
            self.assertFalse(resolve_many(item, [], DAY, decision, require_public=True)[0])
            self.assertEqual(sum(server.calls.values()), 6)  # One MIT, one list, two MIETA and two VRI cards.
            self.assertEqual(max(server.calls.values()), 1)

    def test_display_title_requires_an_exact_mit_record(self):
        client = FixtureClient()
        observed = [
            {"number": "56560-140", "mit_uuid": "different", "title": "Другая запись"},
            {"number": "56560-14", "mit_uuid": "current", "title": "Термопреобразователи сопротивления"},
        ]
        with patch.object(client, "items", return_value=observed) as items:
            self.assertEqual(official_type_title("56560-14", client), "Термопреобразователи сопротивления")
            items.assert_called_once_with("mit", number="56560-14")
        with patch.object(client, "items", return_value=observed[:1]):
            self.assertEqual(official_type_title("56560-14", client), "")

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
        self.assertEqual(conflict.state, "expired")
        self.assertIsNone(resolve(item, [selected], DAY, conflict)[0])

    def test_complete_registration_disambiguates_but_latin_spelling_requires_review(self):
        number = self.first[0]["number"]
        item = etalon(source_id=number, type_number="", registration=number)
        exact = lookup_equipment(item, DAY, self.client)
        self.assertEqual(exact.state, "found")
        self.assertEqual(exact.candidates[0].entry.identifier, number)
        self.assertEqual([query for resource, query in self.client.requests if
                          resource == "mieta" and isinstance(query, dict)], [{"number": number}])
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
            seed_lab(storage)
            results = check([p], storage.confirmations(), [], lookups, laboratory=storage.laboratory())
            self.assertFalse(results[0].blockers)
            schema = Schema()
            payload = format_packets(results, schema)[0][0]
            root, issues, _ = schema.inspect(payload)
            self.assertFalse(issues)
            self.assertEqual([node.text for node in root.findall(f".//{{{NS}}}mieta/{{{NS}}}number")], numbers)

    def test_selected_schemes_survive_xml_and_disjoint_renewal(self):
        numbers = [self.first[0]["number"], self.second[0]["number"]]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            protocol = fixture(root / "source.xlsx", "ТЕСТ-ВЫБРАНЫ-СХЕМЫ", "012B6")
            item = etalon()
            protocol.equipment = [item]
            day = date.fromisoformat(protocol.fields["verification_date"].value)
            early_cards = [(row, {**detail, "cresults": [
                {**detail["cresults"][0], "verification_date": "01.01.2026"}]})
                           for row, detail in (self.first, self.second)]
            current = lookup_equipment(item, day, FixtureClient(early_cards))
            self.assertEqual(current.state, "choice")
            storage = Storage(root / "database.sqlite3")
            seed_lab(storage)
            selected = [candidate.entry for candidate in current.candidates if candidate.eligible]
            storage.save_equipment_choices(selected)
            storage.save_equipment_choices([replace(
                selected[0], valid_from="2027-05-01", valid_until="2028-04-30", public_id="200001"
            )])
            self.assertEqual([entry.identifier for entry in resolve_many(item, storage.entries(), day)[0]], numbers)
            lookups = {lookup_key(item, day.isoformat()): current}
            result = check([protocol], storage.confirmations(), storage.entries(), lookups, laboratory=storage.laboratory())
            self.assertFalse(result[0].blockers)
            schema = Schema()
            payload = format_packets(result, schema)[0][0]
            parsed, issues, _ = schema.inspect(payload)
            self.assertFalse(issues)
            self.assertEqual([node.text for node in parsed.findall(f".//{{{NS}}}mieta/{{{NS}}}number")], numbers)

    def test_mieta_rejects_mismatched_linked_verification(self):
        class WrongSerial(FixtureClient):
            def detail(self, resource, record_id):
                result = super().detail(resource, record_id)
                if resource == "vri":
                    result["miInfo"]["etaMI"]["manufactureNum"] = "000000"
                return result

        client = WrongSerial((self.first,))
        found = lookup_equipment(etalon(), DAY, client)
        self.assertIn(("vri", "1-100001"), client.requests)
        self.assertEqual(found.state, "conflict")
        self.assertIn("расходятся со связанной поверкой", found.message)

    def test_full_identifier_does_not_override_serial_or_model(self):
        number = self.first[0]["number"]
        for item in (etalon(registration=number, serial="3342"),
                     etalon(registration=number, name="Термометр ПТСВ-1-1")):
            found = lookup_equipment(item, DAY, self.client)
            self.assertEqual(found.state, "conflict")
            self.assertFalse(found.candidates[0].eligible)

    def test_vri_barometer_uses_exact_identity_without_generic_model_error(self):
        item = Equipment("3745-73 Барометр-анероид метеорологический М-110 № 42", "Оборудование!A10:E10",
                         "3745-73", "42", "Барометр-анероид метеорологический М-110",
                         category="mis", type_number="3745-73")
        row = {"vri_id": "1-572553788", "mit_number": "3745-73", "mi_number": "42",
               "mit_title": "Барометры-анероиды", "mit_notation": "М-110",
               "verification_date": "10.11.2026", "valid_date": "09.11.2027", "applicability": True}
        for modification in (None, "Нет модификации", "нет данных", "Барометры-анероиды,",
                             "М-110 (принадлежит 102)", "M – 110"):
            with self.subTest(modification=modification):
                found = lookup_equipment(item, DAY, FixtureClient(verifications=[{**row, "mi_modification": modification}]))
                self.assertEqual(found.state, "found")
                self.assertEqual(found.candidates[0].entry.registry_serial, "42")
                self.assertEqual(found.candidates[0].entry.category, "mis")
        for changes, state in (({"mi_number": "042"}, "missing"), ({"mit_number": "3745-74"}, "missing"),
                               ({"valid_date": "13.11.2026"}, "expired"), ({"applicability": False}, "conflict")):
            with self.subTest(changes=changes):
                result = lookup_equipment(item, DAY, FixtureClient(verifications=[{**row, **changes}]))
                self.assertEqual(result.state, state)

    def test_vri_reports_real_model_differences_with_source(self):
        item = replace(etalon(), category="mis", name="Термометр ПТСВ-1-2Б")
        row = {"vri_id": "1-111", "mit_number": "32777-06", "mi_number": "003342",
               "verification_date": "14.04.2026", "valid_date": "13.04.2027", "applicability": True}
        for model in ("ПТСВ-1-2", "ПТСВ-1-2А", "ПТСВ-1-3", "ПТСВ-1-2Б/1"):
            with self.subTest(model=model):
                result = lookup_equipment(item, DAY, FixtureClient(verifications=[{**row, "mi_modification": model}]))
                self.assertEqual(result.state, "conflict")
                self.assertIn("Excel: «Термометр ПТСВ-1-2Б»", result.message)
                self.assertIn(f"ФИФ ОЕИ: «{model}»", result.message)
                self.assertIn("VRI, поле «Модификация»", result.message)
        conflict = {**row, "mi_modification": "ПТСВ-1-2А"}
        generic = {**row, "vri_id": "1-222", "mi_modification": "Нет модификации"}
        result = lookup_equipment(item, DAY, FixtureClient(verifications=[conflict, generic]))
        self.assertEqual(result.state, "conflict")
        self.assertFalse(any(candidate.eligible for candidate in result.candidates))

    def test_mieta_uses_explicit_modification_from_linked_record(self):
        class LinkedModel(FixtureClient):
            def detail(self, resource, record_id):
                result = super().detail(resource, record_id)
                if resource == "mieta":
                    result = {**result, "modification": "Нет данных", "minotation": "Термометры сопротивления"}
                if resource == "vri":
                    result["miInfo"]["etaMI"]["modification"] = "ПТСВ-1-3"
                return result

        result = lookup_equipment(etalon(), DAY, LinkedModel((self.first,)))
        self.assertEqual(result.state, "conflict")
        self.assertIn("Excel: «Термометр ПТСВ-1-2»", result.message)
        self.assertIn("ФИФ ОЕИ: «ПТСВ-1-3»", result.message)
        self.assertIn("связанная VRI", result.message)

    def test_selected_new_template_standards_are_verified_and_exported(self):
        second = mieta("100002", "32777.06.2Р.01000002", "ГПС 2", serial="003343")
        cards = (self.first, second)

        def customize(book):
            table, catalog = book["Таблицы"], book["Оборудование"]
            catalog["D1"] = "Тип прибора"
            for index, (number, factory) in enumerate((("32777-06", "003342"), (second[0]["number"], "003343")), 2):
                label = f"{number} Термометр ПТСВ-1-2 № {factory}"
                for column, value in enumerate((number, "Термометр ПТСВ-1-2", factory, "Эталон", label), 1):
                    catalog.cell(index, column, value)
                table[f"A{8 + index}"] = label
            table["L34"] = "Эталон для измерения"
            table["H30"] = "ПТСВ-1-2 №003342"
            table["H31"] = "Другой, неиспользованный эталон №123"
            table["L36"] = "ПТСВ-1-2 №003343"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            protocol = fixture(root / "source.xlsx", "ТЕСТ-ЭТАЛОНЫ", "012B6", day=DAY, customize=customize)
            storage = Storage(root / "database.sqlite3")
            seed_lab(storage)
            client = FixtureClient(cards)
            lookups = {lookup_key(item, DAY.isoformat()): lookup_equipment(item, DAY, client)
                       for item in protocol.equipment}
            self.assertIn(("mieta", {"mitype_num": "32777-06", "factory_num": "003342"}), client.requests)
            self.assertIn(("mieta", {"number": second[0]["number"]}), client.requests)
            result = check([protocol], {}, [], lookups, laboratory=storage.laboratory(), require_public=True)
            self.assertFalse(result[0].blockers)
            schema = Schema()
            payload = format_packets(result, schema)[0][0]
            parsed, issues, _ = schema.inspect(payload)
            self.assertFalse(issues)
            self.assertEqual([node.text for node in parsed.findall(f".//{{{NS}}}mieta/{{{NS}}}number")],
                             [card[0]["number"] for card in cards])

    def test_vri_later_negative_blocks_an_older_positive_and_offline_has_fallback(self):
        item = replace(etalon(), category="mis", name="Термометр ПТСВ-1-2")
        row = {"vri_id": "1-111", "mit_number": "32777-06", "mi_number": "003342", "mi_modification": "ПТСВ-1-2",
               "verification_date": "14.04.2026", "valid_date": "13.04.2027", "applicability": True}
        client = FixtureClient(verifications=[row])
        good = lookup_equipment(item, DAY, client)
        self.assertEqual(good.state, "found")
        self.assertIn(("vri", {"mit_number": "32777-06", "mi_number": "003342",
                               "verification_date_end": "2026-11-23"}), client.requests)
        observed = good.candidates[0].entry
        saved = replace(observed, automatic=False)
        negative = {**row, "vri_id": "1-222", "verification_date": "21.11.2026", "applicability": False}
        conflict = lookup_equipment(item, DAY, FixtureClient(verifications=[row, negative]))
        self.assertEqual(conflict.state, "conflict")
        self.assertIsNone(resolve(item, [saved], DAY, conflict)[0])
        offline = lookup_equipment(item, DAY, FixtureClient(unavailable=True))
        self.assertIs(resolve(item, [saved], DAY, offline)[0], saved)
        self.assertFalse(resolve_many(item, [saved, observed], DAY, offline, require_public=True)[0])
        self.assertTrue(observed.automatic)
        self.assertEqual(len(observed.version_hash), 64)

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
