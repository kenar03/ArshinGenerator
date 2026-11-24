"""Populate an isolated database already initialized by the previous Windows build."""

import json
from pathlib import Path
import sqlite3
import sys


database, folder = map(Path, sys.argv[1:])
assert database.is_file() and folder.is_dir()
old_name = "Контрольный XML.xml"
(folder / old_name).write_bytes(b"<local-upgrade-test/>")
stamp = "2026-01-15T12:00:00+05:00"
evidence = "Изолированное испытание обновления"
identity = ("56560-14", "001B6", "2026-01-15")
with sqlite3.connect(database) as db:
    db.executemany("insert into settings values (?,?)", [
        ("output_folder", str(folder)),
        ("laboratory", json.dumps({"sign_cipher": "ТСТ", "calibration": False})),
    ])
    db.execute("insert into confirmations values (?,?,?,?,?,?)",
               ("protocol", "upgrade-test", '{"verification_type": "2"}', evidence, "Тест", stamp))
    entry = {
        "description": "Учебное средство", "local_id": "10000-10", "serial": "001", "name": "Учебное средство",
        "category": "mis", "identifier": "10000-10", "type_number": "10000-10", "registry_serial": "001",
        "identity_kind": "manufactureNum", "valid_from": "2025-01-01", "valid_until": "2027-01-01",
        "evidence": evidence, "confirmer": "Тест",
    }
    db.execute("insert into equipment(data) values (?)", (json.dumps(entry),))
    db.execute("insert into exports values (?,?,?,?,?,?,?,?,?)", (
        "upgrade-test", stamp, str(folder), json.dumps([old_name]),
        "cc92b2a1f8670d230a405d42f9ad08a729a6a35cab94751d5bb1208cf493e361",
        "2.1.0", "[]", "Тест", "Сформирован",
    ))
    db.execute("insert into export_items values (?,?,?,?)", (
        "upgrade-test", json.dumps(identity), "control", json.dumps({"file": old_name}),
    ))
    db.execute("insert into audit(created,event,data) values (?,?,?)", (stamp, evidence, '"upgrade-test"'))
