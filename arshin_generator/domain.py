from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

Scalar: TypeAlias = str | int | bool | None
READY = "Готов"
REVIEW = "Требует проверки"
ERROR = "Ошибка"
UNSUPPORTED = "Формат не поддерживается"
OUTCOMES = {"pass": "Пригоден", "fail": "Непригоден", "": "Не подтверждён"}


@dataclass(frozen=True)
class Datum:
    value: Scalar
    source: str
    raw: str = ""


@dataclass(frozen=True)
class Equipment:
    description: str
    source: str
    source_id: str = ""
    serial: str = ""
    name: str = ""
    valid_hint: str = ""
    category: str = ""
    type_number: str = ""
    registration: str = ""


@dataclass(frozen=True)
class ElementReading:
    index: int
    status: str
    complete: bool
    source: str
    measurements: tuple = ()


@dataclass
class Instrument:
    serial: str
    raw: str
    source: str
    elements: tuple[ElementReading, ...] = ()
    problem: str = ""


@dataclass
class Protocol:
    path: Path
    digest: str
    reader: str = ""
    number: str = ""
    declared: int | None = None
    fields: dict[str, Datum] = field(default_factory=dict)
    instruments: list[Instrument] = field(default_factory=list)
    equipment: list[Equipment] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    serial_issue: str = ""
    error: str = ""
    unsupported: bool = False
    elements: int = 0
    conclusion: str = ""
    proposed_modification: str = ""
    modification_prefixes: tuple[str, ...] = ()
    evidence: str = ""
    report_outcome: str = ""
    conclusion_outcome: str = ""
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RegistryEntry:
    description: str
    local_id: str
    serial: str
    name: str
    category: str
    identifier: str
    type_number: str
    registry_serial: str
    identity_kind: str
    valid_from: str
    valid_until: str
    evidence: str
    confirmer: str
    confirmed_at: str = ""
    id: int = 0
    public_id: str = ""
    scheme: str = ""
    model: str = ""


@dataclass
class Result:
    protocol: Protocol
    instrument: Instrument
    fields: dict[str, Datum]
    equipment: list[RegistryEntry] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    candidate: str = ""
    shared_issues: tuple[str, ...] = ()

    def value(self, name: str) -> Scalar:
        return self.fields[name].value if name in self.fields else None

    @property
    def status(self) -> str:
        return REVIEW if self.issues or self.shared_issues else READY

    @property
    def blockers(self) -> tuple[str, ...]:
        return self.shared_issues + tuple(self.issues)

    @property
    def identity(self) -> tuple:
        return self.value("type_number"), self.instrument.serial.upper(), self.value("verification_date")
