from contextlib import contextmanager, closing
from dataclasses import asdict, replace
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

from .domain import RegistryEntry
from .confirmations import GUARDS, individual_key, legacy_key, protocol_key, with_guards

PORTABLE_FLAG = "portable.flag"


def data_directory() -> Path:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve().parent
        if (executable / PORTABLE_FLAG).is_file():
            return executable / "data"
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "ArshinGenerator"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


PORTAL_STATES = ("Сформирован", "Загружен", "Подготовлен к публикации", "Ошибка", "Опубликован")
DDL = """
create table if not exists settings (key text primary key, value text not null);
create table if not exists confirmations (
    scope text not null, subject text not null, data text not null, evidence text not null,
    confirmer text not null, confirmed_at text not null, primary key(scope, subject)
);
create table if not exists equipment (
    id integer primary key, data text not null, active integer not null default 1
);
create table if not exists exports (
    id text primary key, created text not null, directory text not null,
    files text not null, schema_hash text not null, rules text not null,
    excluded text not null, confirmer text not null, status text not null
);
create table if not exists export_items (
    export_id text not null references exports(id), identity text not null,
    record_hash text not null, data text not null, primary key(export_id, identity)
);
create index if not exists ix_export_identity on export_items(identity);
create table if not exists audit (
    id integer primary key, created text not null, event text not null, data text not null
);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as db:
            db.executescript(DDL)

    @contextmanager
    def session(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("pragma foreign_keys=on")
            yield db

    def _audit(self, db, event: str, data) -> None:
        db.execute("insert into audit(created,event,data) values (?,?,?)", (now(), event, encode(data)))

    def audit(self, event: str, data) -> None:
        with self.session() as db:
            self._audit(db, event, data)

    def setting(self, key: str) -> str:
        with self.session() as db:
            row = db.execute("select value from settings where key=?", (key,)).fetchone()
        return row[0] if row else ""

    def set_setting(self, key: str, value: str) -> None:
        with self.session() as db:
            db.execute("insert or replace into settings values (?,?)", (key, value))

    def confirmations(self) -> dict:
        with self.session() as db:
            return {
                (r["scope"], r["subject"]): (
                    json.loads(r["data"]), r["evidence"], r["confirmer"], r["confirmed_at"]
                )
                for r in db.execute("select * from confirmations")
            }

    def save_confirmation(self, scope: str, subject: str, fields: dict, evidence: str, confirmer: str) -> None:
        if not evidence.strip() or not confirmer.strip():
            raise ValueError("Укажите основание и имя сотрудника, который проверил сведения.")
        changed = now()
        with self.session() as db:
            db.execute("insert or replace into confirmations values (?,?,?,?,?,?)",
                       (scope, subject, encode(fields), evidence, confirmer, changed))
            self._audit(db, "Подтверждение", [scope, subject, fields, evidence, confirmer])

    def migrate_confirmation(self, protocol) -> None:
        """Recover older confirmation records only from their original workbook snapshot."""
        keys = [("protocol", protocol_key(protocol), None)]
        keys.extend(("instrument", individual_key(protocol, instrument.serial), instrument)
                    for instrument in protocol.instruments if instrument.serial)
        with self.session() as db:
            for scope, subject, instrument in keys:
                row = db.execute("select data,evidence,confirmer,confirmed_at from confirmations where scope=? and subject=?",
                                 (scope, legacy_key(protocol, instrument))).fetchone()
                if row:
                    fields = with_guards(protocol, json.loads(row["data"]), instrument)
                    db.execute("insert or ignore into confirmations values (?,?,?,?,?,?)",
                               (scope, subject, encode(fields), row["evidence"], row["confirmer"], row["confirmed_at"]))

    def save_batch_outcome(self, subjects: dict[str, str], evidence: str, confirmer: str) -> None:
        if not subjects or not evidence.strip() or not confirmer.strip():
            raise ValueError("Укажите приборы, основание и имя сотрудника.")
        changed = now()
        with self.session() as db:
            for subject, guard in subjects.items():
                row = db.execute("select data from confirmations where scope='instrument' and subject=?",
                                 (subject,)).fetchone()
                fields = json.loads(row[0]) if row else {}
                if fields.get("outcome") and fields.get(GUARDS, {}).get("outcome") == guard:
                    raise ValueError("Один из результатов уже подтверждён. Повторите проверку.")
                fields["outcome"] = "pass"
                fields.setdefault(GUARDS, {})["outcome"] = guard
                db.execute("insert or replace into confirmations values (?,?,?,?,?,?)",
                           ("instrument", subject, encode(fields), evidence, confirmer, changed))
            self._audit(db, "Заключение подтверждено для партии", [subjects, evidence, confirmer])

    def entries(self) -> list[RegistryEntry]:
        with self.session() as db:
            return [
                RegistryEntry(**json.loads(row["data"]), id=row["id"])
                for row in db.execute("select id,data from equipment where active=1 order by id")
            ]

    def save_equipment(self, entry: RegistryEntry) -> None:
        item = asdict(replace(entry, confirmed_at=now()))
        item.pop("id")
        with self.session() as db:
            if entry.id:
                db.execute("update equipment set active=0 where id=?", (entry.id,))
            db.execute("insert into equipment(data) values (?)", (encode(item),))
            self._audit(db, "Подтверждение средства поверки", item)

    def save_equipment_choice(self, entry: RegistryEntry) -> None:
        item = asdict(replace(entry, confirmed_at=now()))
        item.pop("id")
        with self.session() as db:
            for row in db.execute("select id,data from equipment where active=1").fetchall():
                previous = json.loads(row["data"])
                if (all(previous.get(key) == item[key] for key in ("local_id", "serial", "name", "category"))
                        and previous["valid_from"] <= entry.valid_until and entry.valid_from <= previous["valid_until"]):
                    db.execute("update equipment set active=0 where id=?", (row["id"],))
            db.execute("insert into equipment(data) values (?)", (encode(item),))
            self._audit(db, "Выбор публичной карточки", item)

    def archive_equipment(self, entry: RegistryEntry, reason: str) -> None:
        with self.session() as db:
            db.execute("update equipment set active=0 where id=?", (entry.id,))
            self._audit(db, "Отзыв подтверждения средства поверки", [asdict(entry), reason])

    def seen(self, identities: list[tuple]) -> set[tuple]:
        known = set()
        with self.session() as db:
            for identity in identities:
                if db.execute("select 1 from export_items where identity=? limit 1", (encode(identity),)).fetchone():
                    known.add(identity)
        return known

    def record_export(self, batch_id, directory, files, schema_hash, rules, excluded, confirmer, items):
        with self.session() as db:
            db.execute("insert into exports values (?,?,?,?,?,?,?,?,?)", (
                batch_id, now(), str(directory), encode(files), schema_hash, rules,
                encode(excluded), confirmer, PORTAL_STATES[0]
            ))
            db.executemany("insert into export_items values (?,?,?,?)", (
                (batch_id, encode(identity), digest, encode(data)) for identity, digest, data in items
            ))
            self._audit(db, "XML сформирован", [batch_id, files, excluded])

    def history(self) -> list[dict]:
        with self.session() as db:
            return [dict(row) for row in db.execute("select * from exports order by created desc, id desc")]

    def export_context(self, batch_id: str) -> dict:
        with self.session() as db:
            result = {}
            for row in db.execute("select data from export_items where export_id=?", (batch_id,)):
                data = json.loads(row["data"])
                key = (data["serial"]["serial"], data["fields"]["verification_date"]["value"],
                       data["fields"]["type_number"]["value"])
                result[key] = data["protocol"]
            return result

    def change_status(self, batch_id: str, status: str, evidence: str, confirmer: str) -> None:
        if status not in PORTAL_STATES or not evidence.strip() or not confirmer.strip():
            raise ValueError("Выберите статус и укажите подтверждение из личного кабинета и имя сотрудника.")
        with self.session() as db:
            db.execute("update exports set status=? where id=?", (status, batch_id))
            self._audit(db, "Статус пакета подтверждён вручную", [batch_id, status, evidence, confirmer])

    def backup(self, path: Path) -> None:
        if path.exists():
            raise ValueError("Выберите новое имя файла резервной копии.")
        with self.session() as db, closing(sqlite3.connect(path)) as target:
            db.backup(target)
