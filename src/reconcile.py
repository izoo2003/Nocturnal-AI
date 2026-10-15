"""Deterministic three-way reconciliation. No language model and no hidden totals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from pydantic import BaseModel, Field

from src.config import RulesConfig
from src.loader import canonical_uom
from src.review import ensure_all_resolved

_NON_ALNUM = re.compile(r"[^a-z0-9&]+")
_ITEM_NOISE = re.compile(r"[^a-z0-9]+")


class UnresolvedItemsError(RuntimeError):
    """Raised when unclear register rows have not been confirmed."""


class SourceFact(BaseModel):
    pointer: str
    display: str
    qty: float | None = None
    uom: str | None = None
    receipt_date: str | None = None
    lot: str | None = None
    supplier: str | None = None
    item: str | None = None


class FieldBreak(BaseModel):
    field: str
    reg: str | None = None
    erp: str | None = None
    excel: str | None = None


class Outcome(BaseModel):
    entity_key: str
    status: str
    fields: list[FieldBreak] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reg: SourceFact | None = None
    erp: SourceFact | None = None
    excel: SourceFact | None = None
    duplicate_excel: list[SourceFact] = Field(default_factory=list)
    entered_by: str | None = None
    entered_at: str | None = None
    is_late_entry: bool = False
    late_entry_reason: str | None = None
    remarks: str | None = None
    pointers: list[str] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    month: str
    outcomes: list[Outcome]
    hygiene_warnings: list[str] = Field(default_factory=list)


@dataclass
class Canon:
    pointer: str
    entity_key: str
    grn_no: str | None
    receipt_date: str
    supplier_raw: str
    supplier_key: str
    item_raw: str
    item_key: str
    lot: str
    qty: float
    uom: str
    uom_raw: str
    remarks: str | None = None
    is_duplicate: bool = False
    is_return: bool = False
    date_warning: str | None = None
    entered_by: str | None = None
    entered_at: str | None = None
    is_late_entry: bool = False
    late_entry_reason: str | None = None
    netted_from: list[str] = field(default_factory=list)


def run_reconciliation(
    reg_rows: list[Any],
    erp_rows: list[Any],
    store_rows: list[Any],
    rules_config: RulesConfig,
    month: str = "2026-09",
) -> ReconciliationReport:
    try:
        ensure_all_resolved()
    except ValueError as exc:
        raise UnresolvedItemsError(str(exc)) from exc

    register = [_from_register(row, month) for row in reg_rows]
    erp = [_from_erp(row) for row in erp_rows]
    excel = [_from_excel(row) for row in store_rows]
    hygiene = [row.date_warning for row in excel if row.date_warning]
    if rules_config.behavior.auto_net_returns_by_lot:
        excel = _net_returns(excel)

    grouped = _group(register, erp, excel)
    outcomes = [
        _classify(key, grouped[key], rules_config)
        for key in sorted(grouped, key=_entity_sort)
    ]
    return ReconciliationReport(month=month, outcomes=outcomes, hygiene_warnings=hygiene)


def _group(
    register: list[Canon],
    erp: list[Canon],
    excel: list[Canon],
) -> dict[str, dict[str, list[Canon]]]:
    grouped: dict[str, dict[str, list[Canon]]] = {}
    for source, rows in (("register", register), ("erp", erp), ("excel", excel)):
        for row in rows:
            grouped.setdefault(row.entity_key, {"register": [], "erp": [], "excel": []})
            grouped[row.entity_key][source].append(row)
    return grouped


def _net_returns(excel: list[Canon]) -> list[Canon]:
    hosts = {row.lot: row for row in excel if not row.is_return and row.grn_no}
    kept: list[Canon] = []
    for row in excel:
        if not row.is_return:
            kept.append(row)
            continue
        host = hosts.get(row.lot)
        if host is None:
            kept.append(row)
            continue
        host.qty += row.qty
        host.netted_from.append(row.pointer)
    return kept


def _classify(entity_key: str, bucket: dict[str, list[Canon]], rules: RulesConfig) -> Outcome:
    register = bucket["register"][0] if bucket["register"] else None
    erp_row = bucket["erp"][0] if bucket["erp"] else None
    excel_rows = bucket["excel"]
    primary = next((row for row in excel_rows if not row.is_duplicate), None)
    duplicates = [row for row in excel_rows if row.is_duplicate]
    present = [row for row in (register, erp_row, primary) if row is not None]
    pointers = [row.pointer for row in present]
    pointers.extend(row.pointer for row in duplicates)
    if primary is not None:
        pointers.extend(primary.netted_from)

    outcome = Outcome(
        entity_key=entity_key,
        status="MATCHED",
        reg=_fact(register, rules),
        erp=_fact(erp_row, rules),
        excel=_fact(primary, rules),
        duplicate_excel=[_fact(row, rules) for row in duplicates if _fact(row, rules)],
        entered_by=erp_row.entered_by if erp_row else None,
        entered_at=erp_row.entered_at if erp_row else None,
        is_late_entry=bool(erp_row and erp_row.is_late_entry),
        late_entry_reason=erp_row.late_entry_reason if erp_row else None,
        remarks=primary.remarks if primary else None,
        pointers=pointers,
    )
    if register and register.date_warning:
        outcome.warnings.append(register.date_warning)
    if primary and primary.date_warning:
        outcome.warnings.append(primary.date_warning)
    if len(present) < 3:
        outcome.status = "MISSING_FROM_SOURCE"
        return outcome

    breaks: list[FieldBreak] = []
    date_state = _date_state(register, erp_row, primary, rules.tolerances.date_days)
    if date_state == "mismatch":
        breaks.append(_break("date", register, erp_row, primary, rules, "date"))
    elif date_state == "warning":
        outcome.warnings.append(
            f"date within {rules.tolerances.date_days} day tolerance: "
            f"{register.receipt_date} vs {erp_row.receipt_date} vs {primary.receipt_date}"
        )
    if not _same(register.lot, erp_row.lot, primary.lot):
        breaks.append(_break("lot_no", register, erp_row, primary, rules, "lot"))
    if not _same(register.supplier_key, erp_row.supplier_key, primary.supplier_key):
        breaks.append(_break("supplier", register, erp_row, primary, rules, "supplier"))
    if not _same(register.item_key, erp_row.item_key, primary.item_key):
        breaks.append(_break("item_desc", register, erp_row, primary, rules, "item"))
    qty_field = _qty_field(register, erp_row, primary, rules)
    if qty_field:
        breaks.append(_break(qty_field, register, erp_row, primary, rules, "qty"))
    if breaks:
        outcome.status = "MISMATCHED"
        outcome.fields = breaks
        return outcome
    if duplicates:
        outcome.status = "MATCHED_WITH_DUPLICATE"
    return outcome


def _date_state(register: Canon, erp_row: Canon, excel: Canon, tolerance_days: int) -> str:
    dates = [
        datetime.strptime(row.receipt_date, "%Y-%m-%d").date()
        for row in (register, erp_row, excel)
    ]
    worst = max(abs((left - right).days) for left in dates for right in dates)
    if worst == 0:
        return "match"
    if worst <= tolerance_days:
        return "warning"
    return "mismatch"


def _qty_field(register: Canon, erp_row: Canon, excel: Canon, rules: RulesConfig) -> str | None:
    rows = (register, erp_row, excel)
    units = {row.uom for row in rows}
    comparable = all(_pair_matches(left, right, rules) for left in rows for right in rows)
    if comparable:
        return None
    if units <= {"YDS", "M"} and len(units) > 1:
        return "uom_and_qty"
    return "qty"


def _pair_matches(left: Canon, right: Canon, rules: RulesConfig) -> bool:
    factor = rules.unit_conversions.yds_to_m
    left_qty, left_uom = _comparable_qty(left, factor)
    right_qty, right_uom = _comparable_qty(right, factor)
    if left_uom != right_uom:
        return False
    span = max(abs(left_qty), abs(right_qty), 1e-9)
    allowed = span * (rules.tolerances.qty_pct / 100.0)
    return abs(left_qty - right_qty) <= max(allowed, 1e-6)


def _comparable_qty(row: Canon, yds_to_m: float) -> tuple[float, str]:
    if row.uom == "YDS":
        return row.qty * yds_to_m, "M"
    return row.qty, row.uom


def _break(field: str, register: Canon, erp_row: Canon, excel: Canon, rules: RulesConfig, kind: str) -> FieldBreak:
    return FieldBreak(
        field=field,
        reg=_show(register, rules, kind),
        erp=_show(erp_row, rules, kind),
        excel=_show(excel, rules, kind),
    )


def _show(row: Canon, rules: RulesConfig, kind: str) -> str:
    if kind == "date":
        return row.receipt_date
    if kind == "lot":
        return row.lot
    if kind == "supplier":
        return row.supplier_raw
    if kind == "item":
        return row.item_raw
    shown = _qty_text(row.qty, row.uom)
    if row.uom == "YDS":
        meters = row.qty * rules.unit_conversions.yds_to_m
        return f"{shown} (== {_qty_text(meters, 'M')})"
    return shown


def _fact(row: Canon | None, rules: RulesConfig) -> SourceFact | None:
    if row is None:
        return None
    return SourceFact(
        pointer=row.pointer,
        display=_show(row, rules, "qty"),
        qty=row.qty,
        uom=row.uom,
        receipt_date=row.receipt_date,
        lot=row.lot,
        supplier=row.supplier_raw,
        item=row.item_raw,
    )


def _from_register(row: Any, month: str) -> Canon:
    unclear = bool(_value(row, "is_unclear"))
    qty = _value(row, "resolved_qty") if unclear else _value(row, "parsed_qty")
    if qty is None:
        raise UnresolvedItemsError(
            f"Register {_value(row, 'grn_no')} has no quantity to reconcile"
        )
    page_no = int(_value(row, "page_no"))
    row_no = int(_value(row, "row_no"))
    grn_no = str(_value(row, "grn_no"))
    supplier = str(_value(row, "supplier") or "")
    item = str(_value(row, "item_desc") or "")
    uom_raw = str(_value(row, "uom") or "")
    return Canon(
        pointer=f"[Reg P{page_no}:R{row_no:02d}]",
        entity_key=grn_no,
        grn_no=grn_no,
        receipt_date=_register_date(str(_value(row, "raw_date")), month),
        supplier_raw=supplier,
        supplier_key=_supplier_key(supplier),
        item_raw=item,
        item_key=_item_key(item),
        lot=str(_value(row, "lot_no") or ""),
        qty=float(qty),
        uom=canonical_uom(uom_raw),
        uom_raw=uom_raw,
    )


def _from_erp(row: Any) -> Canon:
    grn_no = str(_value(row, "grn_no"))
    entered_at = _value(row, "entered_at")
    entered_text = entered_at.isoformat(sep=" ", timespec="minutes") if isinstance(entered_at, datetime) else str(entered_at or "")
    supplier = str(_value(row, "supplier_name") or "")
    item = str(_value(row, "item_desc") or "")
    uom_raw = str(_value(row, "uom") or "")
    return Canon(
        pointer=str(_value(row, "source_pointer")),
        entity_key=grn_no,
        grn_no=grn_no,
        receipt_date=str(_value(row, "grn_date")),
        supplier_raw=supplier,
        supplier_key=_supplier_key(supplier),
        item_raw=item,
        item_key=_item_key(item),
        lot=str(_value(row, "lot_no") or ""),
        qty=float(_value(row, "qty")),
        uom=canonical_uom(uom_raw),
        uom_raw=uom_raw,
        entered_by=str(_value(row, "entered_by") or ""),
        entered_at=entered_text,
        is_late_entry=bool(_value(row, "is_late_entry")),
        late_entry_reason=_value(row, "late_entry_reason"),
    )


def _from_excel(row: Any) -> Canon:
    grn_no = _value(row, "grn_no")
    grn_text = str(grn_no) if grn_no else None
    entity_key = str(_value(row, "entity_key") or grn_text)
    supplier = str(_value(row, "supplier") or "")
    item = str(_value(row, "material") or "")
    uom_raw = str(_value(row, "uom") or "")
    return Canon(
        pointer=str(_value(row, "source_pointer")),
        entity_key=entity_key,
        grn_no=grn_text,
        receipt_date=str(_value(row, "receipt_date")),
        supplier_raw=supplier,
        supplier_key=_supplier_key(supplier),
        item_raw=item,
        item_key=_item_key(item),
        lot=str(_value(row, "lot") or ""),
        qty=float(_value(row, "qty")),
        uom=canonical_uom(uom_raw),
        uom_raw=uom_raw,
        remarks=_value(row, "remarks"),
        is_duplicate=bool(_value(row, "is_duplicate")),
        is_return=bool(_value(row, "is_return")),
        date_warning=_value(row, "date_warning"),
    )


def _register_date(raw: str, month: str) -> str:
    if len(raw) == 10 and raw[4] == "-":
        return raw
    day, mon = raw.split("/")
    year = month.split("-")[0]
    return f"{year}-{int(mon):02d}-{int(day):02d}"


def _supplier_key(name: str) -> str:
    cleaned = _NON_ALNUM.sub(" ", name.casefold()).strip()
    aliases = {
        "qasr pkg": "qasr",
        "qasr packaging": "qasr",
        "qasr packaging pvt ltd": "qasr",
        "taraz poly": "taraz",
        "taraz poly industries": "taraz",
        "zarnab chem": "zarnab",
        "zarnab chemicals": "zarnab",
        "zarnab chemical industries pvt ltd": "zarnab",
        "rehbar thread": "rehbar",
        "rehbar threads": "rehbar",
        "saahil pallets": "saahil",
        "saahil pallets & timber": "saahil",
        "nayab labels": "nayab",
        "nayab labels pvt ltd": "nayab",
        "shahtoot": "shahtoot",
        "shahtoot weaving": "shahtoot",
        "shahtoot weaving mills": "shahtoot",
    }
    return aliases.get(cleaned, cleaned)


def _item_key(text: str) -> str:
    return _ITEM_NOISE.sub(" ", text.casefold()).strip()


def _same(*values: str) -> bool:
    return len(set(values)) == 1


def _qty_text(qty: float, uom: str) -> str:
    if abs(qty - round(qty)) < 1e-6:
        return f"{int(round(qty)):,} {uom.lower()}"
    return f"{qty:,.2f} {uom}"


def _entity_sort(entity_key: str) -> tuple[int, str]:
    return (0, entity_key) if entity_key.isdigit() else (1, entity_key)


def _value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key)
