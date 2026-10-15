"""End-to-end September receiving reconciliation."""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from pathlib import Path

from src.completeness import CompletenessError, verify_completeness
from src.config import ROOT, ensure_directories, get_scenario_now, load_rules
from src.db import init_db, list_all_register_rows, save_reconciliation_run
from src.extractor import extract_register_page
from src.loader import load_erp_grn, load_store_sheet
from src.notifier import format_controller_summary, send_failure_alert, send_message
from src.reconcile import UnresolvedItemsError, run_reconciliation
from src.review import run_cli_review

REGISTER_PAGES = (
    ROOT / "kestrel-data" / "t4_register_page1.jpg",
    ROOT / "kestrel-data" / "t4_register_page2.jpg",
)
ERP_PATH = ROOT / "kestrel-data" / "t4_erp_grn.csv"
EXCEL_PATH = ROOT / "kestrel-data" / "t4_store_sheet.xlsx"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    for image_path in REGISTER_PAGES:
        rows = extract_register_page(image_path, force=args.force_ocr)
        logging.info("Extracted %s rows from %s", len(rows), image_path.name)
    if args.interactive_review:
        run_cli_review(auto_resolve_demo=False)
    elif args.auto_resolve_demo:
        run_cli_review(auto_resolve_demo=True)

    rules = load_rules()
    register_rows = _register_rows()
    erp_rows = load_erp_grn(ERP_PATH, rules)
    store_rows = load_store_sheet(EXCEL_PATH)
    try:
        report = run_reconciliation(register_rows, erp_rows, store_rows, rules, month=args.month)
        proof_rows = register_rows[:-1] if args.demo_failure else register_rows
        proof = verify_completeness(report, proof_rows, erp_rows, store_rows)
    except (UnresolvedItemsError, CompletenessError) as exc:
        _save_run(args.month, "failed", {"error": str(exc)}, passed=False)
        send_failure_alert(str(exc), force_send=args.force_send)
        raise SystemExit(1) from exc

    payload = {
        "report": report.model_dump(mode="json"),
        "completeness": proof.model_dump(mode="json"),
    }
    _save_run(args.month, "passed", payload, passed=True)
    ensure_directories()
    report_path = ROOT / "outbox" / f"reconciliation_report_{args.month}.json"
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    summary = format_controller_summary(report, proof, get_scenario_now(), month=args.month)
    send_message(summary, message_type="SUMMARY", force_send=args.force_send)
    logging.info("Reconciliation passed and the controller summary was delivered.")


def _register_rows() -> list[dict]:
    conn = init_db()
    try:
        return [dict(row) for row in list_all_register_rows(conn)]
    finally:
        conn.close()


def _save_run(month: str, status: str, payload: dict, passed: bool) -> None:
    conn = init_db()
    try:
        save_reconciliation_run(
            conn,
            run_id=f"{month}:{status}:{uuid.uuid4().hex[:8]}",
            status=status,
            summary_json=json.dumps(payload),
            completeness_passed=passed,
            run_time=get_scenario_now().isoformat(),
        )
    finally:
        conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile the September 2026 receiving register, ERP, and store sheet.")
    parser.add_argument("--month", default="2026-09")
    parser.add_argument("--force-ocr", action="store_true", help="Re-extract register pages even when the image hash is cached.")
    parser.add_argument("--auto-resolve-demo", action="store_true", help="Confirm GRN 4419 as 66 and GRN 4428 as 250.")
    parser.add_argument("--interactive-review", action="store_true", help="Ask for each doubtful quantity in the terminal.")
    parser.add_argument("--demo-failure", action="store_true", help="Drop one register row so the completeness proof fails closed.")
    parser.add_argument("--force-send", action="store_true", help="Send again even when this payload was already delivered.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
