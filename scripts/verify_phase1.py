"""Load the official Task 4 files and check Phase 1 ingestion counts."""

from __future__ import annotations

import json
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_scenario_now, load_rules
from src.db import (
    get_outbound_message,
    init_db,
    insert_register_rows,
    list_unresolved_rows,
    record_human_resolution,
    record_outbound_message,
)
from src.loader import DataFormatWarning, load_erp_grn, load_store_sheet

DATA = ROOT / "kestrel-data"
SNAPSHOT = ROOT / "data" / "ocr_snapshot.json"


def main() -> None:
    rules = load_rules()
    scenario_now = get_scenario_now()
    erp = load_erp_grn(DATA / "t4_erp_grn.csv", rules)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DataFormatWarning)
        store = load_store_sheet(DATA / "t4_store_sheet.xlsx")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rows = snapshot["rows"]

    late = [row for row in erp if row.is_late_entry]
    duplicates = [row for row in store if row.is_duplicate]
    returns = [row for row in store if row.is_return]
    format_warnings = [row for row in store if row.date_warning]
    erp_ids = {row.grn_no for row in erp}

    with tempfile.TemporaryDirectory() as tmp:
        conn = init_db(Path(tmp) / "kestrel_reconcile.db")
        inserted = insert_register_rows(conn, rows)
        unresolved = list_unresolved_rows(conn)
        record_outbound_message(
            conn,
            message_hash="phase1-probe",
            channel="outbox",
            recipient="test",
            payload_text="probe",
            status="sent",
            sent_at=scenario_now.isoformat(),
        )
        stored_message = get_outbound_message(conn, "phase1-probe")
        record_human_resolution(
            conn,
            grn_no="4419",
            field_name="qty",
            raw_ocr_value="60 crossed out, 66 above",
            resolved_value="66",
            reviewer_note="phase1 helper check",
            resolved_by="verify",
            resolved_at=scenario_now.isoformat(),
        )
        after_resolve = list_unresolved_rows(conn)
        conn.close()

    unclear = [row for row in rows if row["is_unclear"]]
    checks = {
        "erp_rows": len(erp) == 28,
        "erp_missing_4421_4429": "4421" not in erp_ids and "4429" not in erp_ids,
        "erp_late_4430_only": [row.grn_no for row in late] == ["4430"],
        "erp_qty_sum": round(sum(row.qty for row in erp), 2) == 115704.28,
        "store_rows": len(store) == 30,
        "store_duplicate_4409": [row.grn_no for row in duplicates] == ["4409"],
        "store_duplicate_pointer": duplicates[0].source_pointer == "[Excel R11]",
        "store_primary_4409_pointer": next(
            row.source_pointer for row in store if row.grn_no == "4409" and not row.is_duplicate
        )
        == "[Excel R10]",
        "store_date_warning_4416": [row.grn_no for row in format_warnings] == ["4416"]
        and format_warnings[0].receipt_date == "2026-09-14"
        and format_warnings[0].sheet_row == 18,
        "store_warning_emitted": any(issubclass(item.category, DataFormatWarning) for item in caught),
        "store_return": [row.entity_key for row in returns] == ["RETURN-L2609-002"]
        and returns[0].qty == -5000
        and returns[0].sheet_row == 31,
        "store_qty_sum": sum(row.qty for row in store) == 109058,
        "snapshot_rows": len(rows) == 28 and inserted == 28,
        "snapshot_unclear": [row["grn_no"] for row in unclear] == ["4419", "4428"],
        "snapshot_unclear_qty_not_guessed": all(row["parsed_qty"] is None for row in unclear)
        and unclear[1]["raw_qty_text"] == "2?? (ink blot)",
        "snapshot_known_deltas": next(row for row in rows if row["grn_no"] == "4412")["lot_no"] == "L2609-012"
        and next(row for row in rows if row["grn_no"] == "4415")["raw_date"] == "12/09"
        and next(row for row in rows if row["grn_no"] == "4417")["parsed_qty"] == 48
        and next(row for row in rows if row["grn_no"] == "4425")["uom"] == "yds",
        "unresolved_before_review": [row["grn_no"] for row in unresolved] == ["4419", "4428"],
        "unresolved_after_4419": [row["grn_no"] for row in after_resolve] == ["4428"],
        "message_hash_stored": stored_message is not None and stored_message["status"] == "sent",
        "rules_yds": rules.unit_conversions.yds_to_m == 0.9144,
    }
    failed = [name for name, ok in checks.items() if not ok]
    print(f"scenario_now={scenario_now.isoformat()}")
    print(f"erp_rows={len(erp)} late={[row.grn_no for row in late]}")
    print(f"store_rows={len(store)} duplicates={[row.source_pointer for row in duplicates]} returns={[row.entity_key for row in returns]}")
    print(f"format_warnings={[row.source_pointer + ' ' + row.grn_no for row in format_warnings]}")
    print(f"snapshot_rows={len(rows)} unresolved={[row['grn_no'] for row in unresolved]}")
    print(f"checks_passed={len(checks) - len(failed)}/{len(checks)}")
    if failed:
        print("FAILED: " + ", ".join(failed))
        raise SystemExit(1)
    print("Phase 1 ingestion checks passed.")


if __name__ == "__main__":
    main()
