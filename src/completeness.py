"""Row-partition and quantity-bridge proof. Fails closed."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from pydantic import BaseModel, Field

from src.config import load_rules

REGISTER_RAW_SUM = 113_734.00
REGISTER_RESOLVED_SUM = 113_984.00
ERP_SUM = 115_704.28
EXCEL_SUM = 109_058.00
ERP_BRIDGE = 1_720.28
EXCEL_BRIDGE = -4_926.00
UOM_ORDER = ("PCS", "BAG", "CONE", "DRUM", "ROLL", "CTN", "BDL", "YDS", "M", "YDS/M")


class CompletenessError(RuntimeError):
    """Raised when a source row or a quantity total does not tie."""


class UomTie(BaseModel):
    uom: str
    register_qty: float
    erp_qty: float
    excel_qty: float


class CompletenessProof(BaseModel):
    register_rows: int
    erp_rows: int
    excel_rows: int
    register_raw_sum: float
    register_resolved_sum: float
    erp_sum: float
    excel_sum: float
    erp_bridge: float
    excel_bridge: float
    erp_bridge_parts: dict[str, float] = Field(default_factory=dict)
    excel_bridge_parts: dict[str, float] = Field(default_factory=dict)
    uom_tie_out: list[UomTie] = Field(default_factory=list)
    passed: bool = True


def verify_completeness(report, reg_rows, erp_rows, store_rows) -> CompletenessProof:
    problems: list[str] = []
    problems.extend(_partition_problems(report, reg_rows, erp_rows, store_rows))

    register_resolved = round(sum(_resolved_qty(row) for row in reg_rows), 2)
    register_raw = round(sum(_resolved_qty(row) for row in reg_rows if not _is_blot(row)), 2)
    erp_sum = round(sum(float(_value(row, "qty")) for row in erp_rows), 2)
    excel_sum = round(sum(float(_value(row, "qty")) for row in store_rows), 2)
    erp_bridge = round(erp_sum - register_resolved, 2)
    excel_bridge = round(excel_sum - register_resolved, 2)
    erp_parts = _erp_bridge_parts(reg_rows, erp_rows)
    excel_parts = _excel_bridge_parts(reg_rows, store_rows)

    _expect(problems, "register raw sum", register_raw, REGISTER_RAW_SUM)
    _expect(problems, "register resolved sum", register_resolved, REGISTER_RESOLVED_SUM)
    _expect(problems, "ERP sum", erp_sum, ERP_SUM)
    _expect(problems, "Excel sum", excel_sum, EXCEL_SUM)
    _expect(problems, "ERP bridge", erp_bridge, ERP_BRIDGE)
    _expect(problems, "Excel bridge", excel_bridge, EXCEL_BRIDGE)
    _expect(problems, "ERP bridge parts", round(sum(erp_parts.values()), 2), erp_bridge)
    _expect(problems, "Excel bridge parts", round(sum(excel_parts.values()), 2), excel_bridge)

    proof = CompletenessProof(
        register_rows=len(reg_rows),
        erp_rows=len(erp_rows),
        excel_rows=len(store_rows),
        register_raw_sum=register_raw,
        register_resolved_sum=register_resolved,
        erp_sum=erp_sum,
        excel_sum=excel_sum,
        erp_bridge=erp_bridge,
        excel_bridge=excel_bridge,
        erp_bridge_parts=erp_parts,
        excel_bridge_parts=excel_parts,
        uom_tie_out=_uom_tie_out(reg_rows, erp_rows, store_rows),
        passed=not problems,
    )
    if problems:
        raise CompletenessError("Completeness failed: " + "; ".join(problems))
    return proof


def _partition_problems(report, reg_rows, erp_rows, store_rows) -> list[str]:
    expected = {
        "register": {_register_pointer(row) for row in reg_rows},
        "erp": {str(_value(row, "source_pointer")) for row in erp_rows},
        "excel": {str(_value(row, "source_pointer")) for row in store_rows},
    }
    seen: dict[str, list[str]] = {"register": [], "erp": [], "excel": []}
    for outcome in report.outcomes:
        for pointer in outcome.pointers:
            seen[_source_name(pointer)].append(pointer)
    problems: list[str] = []
    for source, raw in expected.items():
        assigned = seen[source]
        counts = Counter(assigned)
        missing = raw - set(assigned)
        extra = set(assigned) - raw
        duplicated = {pointer for pointer, count in counts.items() if count > 1}
        if missing:
            problems.append(f"{source} dropped {sorted(missing)}")
        if extra:
            problems.append(f"{source} unexpected {sorted(extra)}")
        if duplicated:
            problems.append(f"{source} repeated {sorted(duplicated)}")
    if len(reg_rows) != 28 or len(expected["register"]) != 28:
        problems.append(f"Register {len(reg_rows)}/28")
    if len(erp_rows) != 28 or len(expected["erp"]) != 28:
        problems.append(f"ERP {len(erp_rows)}/28")
    if len(store_rows) != 30 or len(expected["excel"]) != 30:
        problems.append(f"Excel {len(store_rows)}/30")
    return problems


def _erp_bridge_parts(reg_rows, erp_rows) -> dict[str, float]:
    reg = {_value(row, "grn_no"): _resolved_qty(row) for row in reg_rows}
    erp = {str(_value(row, "grn_no")): float(_value(row, "qty")) for row in erp_rows}
    return {
        "4417": round(erp["4417"] - reg["4417"], 2),
        "4421": round(-reg["4421"], 2),
        "4425": round(erp["4425"] - reg["4425"], 2),
        "4430": round(erp["4430"], 2),
    }


def _excel_bridge_parts(reg_rows, store_rows) -> dict[str, float]:
    reg = {_value(row, "grn_no"): _resolved_qty(row) for row in reg_rows}
    duplicate = next(row for row in store_rows if _value(row, "is_duplicate"))
    excel_4419 = next(row for row in store_rows if str(_value(row, "grn_no")) == "4419")
    returned = next(row for row in store_rows if _value(row, "is_return"))
    return {
        "4409_duplicate": round(float(_value(duplicate, "qty")), 2),
        "4419": round(float(_value(excel_4419, "qty")) - reg["4419"], 2),
        "return": round(float(_value(returned, "qty")), 2),
    }


def _uom_tie_out(reg_rows, erp_rows, store_rows) -> list[UomTie]:
    factor = load_rules().unit_conversions.yds_to_m
    totals = {uom: {"register": 0.0, "erp": 0.0, "excel": 0.0} for uom in UOM_ORDER}
    _add_uom(totals, "register", reg_rows, "uom", factor, resolved=True)
    _add_uom(totals, "erp", erp_rows, "uom", factor, resolved=False)
    _add_uom(totals, "excel", store_rows, "uom", factor, resolved=False)
    return [
        UomTie(
            uom=uom,
            register_qty=round(totals[uom]["register"], 2),
            erp_qty=round(totals[uom]["erp"], 2),
            excel_qty=round(totals[uom]["excel"], 2),
        )
        for uom in UOM_ORDER
    ]


def _add_uom(totals, source: str, rows, uom_key: str, factor: float, resolved: bool) -> None:
    from src.loader import canonical_uom

    for row in rows:
        qty = _resolved_qty(row) if resolved else float(_value(row, "qty"))
        uom = canonical_uom(_value(row, uom_key))
        if uom in totals:
            totals[uom][source] += qty
        if uom == "YDS":
            totals["YDS/M"][source] += qty * factor
        elif uom == "M":
            totals["YDS/M"][source] += qty


def _resolved_qty(row: Any) -> float:
    if bool(_value(row, "is_unclear")):
        return float(_value(row, "resolved_qty"))
    return float(_value(row, "parsed_qty"))


def _is_blot(row: Any) -> bool:
    text = str(_value(row, "raw_qty_text") or "").lower()
    return "blot" in text or "??" in text


def _register_pointer(row: Any) -> str:
    return f"[Reg P{int(_value(row, 'page_no'))}:R{int(_value(row, 'row_no')):02d}]"


def _source_name(pointer: str) -> str:
    if pointer.startswith("[Reg"):
        return "register"
    if pointer.startswith("[ERP"):
        return "erp"
    if pointer.startswith("[Excel"):
        return "excel"
    raise CompletenessError(f"Unrecognized source pointer {pointer}")


def _expect(problems: list[str], label: str, actual: float, expected: float) -> None:
    if abs(actual - expected) >= 1e-6:
        problems.append(f"{label} {actual} != {expected}")


def _value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key)
