from collections import Counter, defaultdict
from contextlib import contextmanager, closing
from dataclasses import asdict, replace
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

from .domain import RegistryEntry
from .laboratory import SETTING as LABORATORY_SETTING
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
create table if not exists registry_cache (
    kind text not null, lookup_key text not null, checked_at real not null, data text not null,
    primary key(kind, lookup_key)
);
create index if not exists ix_registry_cache_checked on registry_cache(checked_at);
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

    def xml_output(self) -> tuple[bool, str]:
        with self.session() as db:
            settings = dict(db.execute("select key, value from settings where key in (?, ?)",
                                       ("xml_output_mode", "xml_auto_folder")))
        return settings.get("xml_output_mode") == "automatic", settings.get("xml_auto_folder", "")

    def save_xml_output(self, automatic: bool, folder: str) -> None:
        with self.session() as db:
            db.executemany("insert or replace into settings values (?,?)",
                           (("xml_output_mode", "automatic" if automatic else "manual"),
                            ("xml_auto_folder", folder)))

    def laboratory(self) -> dict:
        return json.loads(self.setting(LABORATORY_SETTING) or "{}")

    def save_laboratory(self, values: dict, confirmer: str) -> None:
        with self.session() as db:
            db.execute("insert or replace into settings values (?,?)", (LABORATORY_SETTING, encode(values)))
            self._audit(db, "Настройки лаборатории", [values, confirmer])

    def registry_cache(self, since, until):
        with self.session() as db:
            return db.execute("select * from registry_cache where checked_at > ? and checked_at <= ?",
                              (since, until)).fetchall()

    def save_registry_cache(self, checked_at, verified, invalidated):
        with self.session() as db:
            db.executemany("delete from registry_cache where kind=? and lookup_key=? and checked_at <= ?",
                           ((kind, encode(key), checked_at) for kind, key in invalidated))
            db.executemany("""
                insert into registry_cache values (?,?,?,?)
                on conflict(kind, lookup_key) do update set checked_at=excluded.checked_at, data=excluded.data
                where excluded.checked_at >= registry_cache.checked_at
            """, ((kind, encode(key), checked_at, encode(asdict(value))) for kind, key, value in verified))

    def confirmations(self) -> dict:
        with self.session() as db:
            return {
                (r["scope"], r["subject"]): (
                    json.loads(r["data"]), r["evidence"], r["confirmer"], r["confirmed_at"]
                )
                for r in db.execute("select * from confirmations")
            }

    def save_confirmation(self, scope: str, subject: str, fields: dict, evidence: str, confirmer: str) -> None:
        self.save_confirmations([(scope, subject, fields, evidence, confirmer)])

    def save_confirmations(self, rows) -> None:
        rows = list(rows)
        if any(not evidence.strip() or not confirmer.strip() for _, _, _, evidence, confirmer in rows):
            raise ValueError("Укажите основание и имя сотрудника, который проверил сведения.")
        changed = now()
        with self.session() as db:
            for scope, subject, fields, evidence, confirmer in rows:
                db.execute("insert or replace into confirmations values (?,?,?,?,?,?)",
                           (scope, subject, encode(fields), evidence, confirmer, changed))
                self._audit(db, "Подтверждение", [scope, subject, fields, evidence, confirmer])

    def migrate_confirmation(self, protocol) -> None:
        """Recover older confirmation records only from their original workbook snapshot."""
        with self.session() as db:
            self._migrate_confirmation(db, protocol)

    @staticmethod
    def _migrate_confirmation(db, protocol):
        keys = [("protocol", protocol_key(protocol), None)]
        keys.extend(("instrument", individual_key(protocol, instrument.serial), instrument)
                    for instrument in protocol.instruments if instrument.serial)
        for scope, subject, instrument in keys:
            row = db.execute("select data,evidence,confirmer,confirmed_at from confirmations where scope=? and subject=?",
                             (scope, legacy_key(protocol, instrument))).fetchone()
            if row:
                fields = with_guards(protocol, json.loads(row["data"]), instrument)
                db.execute("insert or ignore into confirmations values (?,?,?,?,?,?)",
                           (scope, subject, encode(fields), row["evidence"], row["confirmer"], row["confirmed_at"]))

    def record_imports(self, protocols):
        with self.session() as db:
            for protocol in protocols:
                if not protocol.error:
                    self._migrate_confirmation(db, protocol)
                self._audit(db, "Чтение протокола", {
                    "path": str(protocol.path), "digest": protocol.digest, "reader": protocol.reader,
                    "number": protocol.number, "error": protocol.error,
                })

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
        item = asdict(replace(entry, confirmed_at=now(), automatic=False))
        item.pop("id")
        with self.session() as db:
            for row in db.execute("select id,data from equipment where active=1").fetchall():
                previous = json.loads(row["data"])
                if (all(previous.get(key) == item[key] for key in ("local_id", "serial", "name", "category"))
                        and previous["valid_from"] <= entry.valid_until and entry.valid_from <= previous["valid_until"]):
                    db.execute("update equipment set active=0 where id=?", (row["id"],))
            db.execute("insert into equipment(data) values (?)", (encode(item),))
            self._audit(db, "Выбор публичной карточки", item)

    def save_equipment_choices(self, entries: list[RegistryEntry]) -> None:
        if not entries:
            raise ValueError("Выберите хотя бы одну карточку.")
        keys = {(item.local_id, item.serial, item.name, item.category) for item in entries}
        ids = tuple(dict.fromkeys(item.public_id for item in entries))
        if len(keys) != 1 or len(ids) != len(entries) or not all(ids):
            raise ValueError("Карточки должны принадлежать одной позиции протокола.")
        if len(ids) > 1 and entries[0].category != "mieta":
            raise ValueError("Для обычного средства выберите одну поверку.")
        start = max(entry.valid_from for entry in entries)
        end = min(entry.valid_until for entry in entries)
        if not start or not end or start > end:
            raise ValueError("У выбранных карточек нет общего срока действия.")
        changed = now()
        with self.session() as db:
            prior_groups = defaultdict(list)
            for row in db.execute("select id,data from equipment where active=1").fetchall():
                previous = json.loads(row["data"])
                if tuple(previous.get(key) for key in ("local_id", "serial", "name", "category")) in keys:
                    group = ((previous["confirmed_at"], tuple(previous["choice_ids"]))
                             if previous.get("choice_ids") else row["id"])
                    prior_groups[group].append((row["id"], previous))
            for group in prior_groups.values():
                if max(item["valid_from"] for _, item in group) <= end and start <= min(
                    item["valid_until"] for _, item in group
                ):
                    db.executemany("update equipment set active=0 where id=?", [(i,) for i, _ in group])
            for entry in entries:
                data = asdict(replace(entry, confirmed_at=changed, choice_ids=ids, automatic=False))
                data.pop("id")
                db.execute("insert into equipment(data) values (?)", (encode(data),))
                self._audit(db, "Выбор публичной карточки", data)

    def save_registry_checks(self, entries) -> None:
        with self.session() as db:
            versions = {}
            for row in db.execute("select id,data from equipment where active=1"):
                item = json.loads(row["data"])
                if item.get("automatic"):
                    versions[(item["local_id"], item["serial"], item["version_hash"])] = row["id"]
            for entry in entries:
                if not entry.automatic or not entry.version_hash:
                    continue
                data = asdict(entry)
                data.pop("id")
                key = entry.local_id, entry.serial, entry.version_hash
                if key in versions:
                    db.execute("update equipment set data=? where id=?", (encode(data), versions[key]))
                else:
                    cursor = db.execute("insert into equipment(data) values (?)", (encode(data),))
                    versions[key] = cursor.lastrowid

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
        occurrences = Counter()
        records = []
        for identity, digest, data in items:
            occurrences[identity] += 1
            key = identity if occurrences[identity] == 1 else (*identity, occurrences[identity])
            records.append((batch_id, encode(key), digest, encode(data)))
        with self.session() as db:
            db.execute("insert into exports values (?,?,?,?,?,?,?,?,?)", (
                batch_id, now(), str(directory), encode(files), schema_hash, rules,
                encode(excluded), confirmer, PORTAL_STATES[0]
            ))
            db.executemany("insert into export_items values (?,?,?,?)", records)
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
                if "position" in data:
                    key = (data["file"], data["position"], *key)
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
