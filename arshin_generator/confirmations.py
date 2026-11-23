from hashlib import sha256
import json

LEGACY_RULES = "1.0.0"
GUARDS = "__guards"


def _encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _value(protocol, key):
    datum = protocol.fields.get(key)
    return datum.value if datum else None


def protocol_key(protocol) -> str:
    identity = [protocol.reader, protocol.number.strip().casefold(),
                _value(protocol, "verification_date"), _value(protocol, "type_number")]
    if not all(identity):
        identity.append(protocol.digest)
    return "protocol-v2:" + _encoded(identity)


def individual_key(protocol, number: str) -> str:
    return protocol_key(protocol) + ":" + _encoded(number.upper())


def _guard(protocol, key, instrument):
    values = [_value(protocol, key)]
    if key in {"one_result", "valid_date", "valid_date_absent"}:
        values.extend(_value(protocol, name) for name in ("marking", "wiring", "verification_type", "interval"))
    if key in {"calibration", "outcome", "reasons"}:
        values.extend([protocol.conclusion, protocol.report_outcome, protocol.conclusion_outcome, _value(protocol, "reasons"),
                       _value(protocol, "wiring"), _value(protocol, "method"), _value(protocol, "marking")])
    if key in {"outcome", "reasons"} and instrument:
        values.append([(item.index, item.status, item.complete, item.measurements) for item in instrument.elements])
    if key in {"explicit_serials", "sign_pass", "sign_mi"} and instrument is None:
        values.append([item.serial for item in protocol.instruments])
    if key == "explicit_serials":
        values.extend([_value(protocol, "serial_list"), protocol.declared])
    return sha256(_encoded(values).encode("utf-8")).hexdigest()


def with_guards(protocol, fields: dict, instrument=None) -> dict:
    return {**fields, GUARDS: {key: _guard(protocol, key, instrument) for key in fields if key != GUARDS}}


def get_confirmation(confirmations: dict, scope: str, protocol, instrument=None):
    subject = individual_key(protocol, instrument.serial) if instrument else protocol_key(protocol)
    item = confirmations.get((scope, subject))
    if item is None:
        item = confirmations.get((scope, legacy_key(protocol, instrument)))
    if item is None:
        return None
    fields, evidence, person, timestamp = item
    guards = fields.get(GUARDS, {})
    applicable = {key: value for key, value in fields.items() if key != GUARDS and
                  (key not in guards or guards[key] == _guard(protocol, key, instrument))}
    return applicable, evidence, person, timestamp


def legacy_key(protocol, instrument=None) -> str:
    return f"{LEGACY_RULES}:{protocol.reader}:{protocol.digest}:{instrument.serial if instrument else ''}"
