"""Focused review for unclear register rows only."""

from __future__ import annotations

import logging
import os

from rich.console import Console

from pipeline.config import get_scenario_now
from pipeline.db import init_db, list_unresolved_rows, record_human_resolution

logger = logging.getLogger(__name__)
console = Console()

DEMO_RESOLUTIONS = {"4419": 66.0, "4428": 250.0}
UNRESOLVED_MESSAGE = "Cannot proceed to reconciliation: Unresolved doubtful rows exist."


def get_pending_reviews() -> list[dict]:
    conn = init_db()
    try:
        return [_row_dict(row) for row in list_unresolved_rows(conn)]
    finally:
        conn.close()


def run_cli_review(auto_resolve_demo: bool = False) -> None:
    conn = init_db()
    try:
        pending = list_unresolved_rows(conn)
        if not pending:
            console.print("No doubtful rows are waiting for review.")
            return
        if auto_resolve_demo:
            _apply_demo_resolutions(conn, pending)
            return
        for row in pending:
            _prompt_for_row(conn, row)
    finally:
        conn.close()


def ensure_all_resolved() -> None:
    if get_pending_reviews():
        raise ValueError(UNRESOLVED_MESSAGE)


def _apply_demo_resolutions(conn, pending) -> None:
    logger.warning(
        "Demo resolution was applied. GRN 4419 was set to 66.0 and GRN 4428 was set to 250.0 "
        "without a person confirming the crops."
    )
    stamped = get_scenario_now().isoformat()
    for row in pending:
        grn_no = str(row["grn_no"])
        if grn_no not in DEMO_RESOLUTIONS:
            raise ValueError(f"Demo resolution has no verified quantity for GRN {grn_no}")
        record_human_resolution(
            conn,
            grn_no=grn_no,
            field_name=row["unclear_field"] or "qty",
            raw_ocr_value=row["raw_qty_text"],
            resolved_value=f"{DEMO_RESOLUTIONS[grn_no]:.1f}",
            reviewer_note="auto-resolve-demo",
            resolved_by="demo",
            resolved_at=stamped,
        )


def _prompt_for_row(conn, row) -> None:
    field = row["unclear_field"] or "qty"
    console.print(f"GRN {row['grn_no']}")
    console.print(f"Supplier: {row['supplier']}")
    console.print(f"Item: {row['item_desc']}")
    console.print(f"Date: {row['raw_date']}")
    console.print(f"Unclear reason: {row['unclear_reason']}")
    console.print(f"Crop: {row['crop_path']}")
    value = console.input(f"Enter the verified value for {field} (or press Enter to skip): ").strip()
    if not value:
        return
    record_human_resolution(
        conn,
        grn_no=str(row["grn_no"]),
        field_name=field,
        raw_ocr_value=row["raw_qty_text"],
        resolved_value=value,
        reviewer_note=None,
        resolved_by=_reviewer_name(),
        resolved_at=get_scenario_now().isoformat(),
    )


def _reviewer_name() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "reviewer"


def _row_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}
