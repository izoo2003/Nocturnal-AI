"""Streamlit review desk for the September 2026 receiving reconciliation."""

from __future__ import annotations

import html
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from src.completeness import CompletenessError, verify_completeness
from src.config import ROOT, RULES_PATH, get_scenario_now, load_rules
from src.db import (
    get_processed_page,
    init_db,
    list_official_register_rows,
    list_register_rows,
    record_human_resolution,
    save_reconciliation_run,
)
from src.extractor import extract_register_image, extract_register_page, sha256_file
from src.loader import load_erp_grn, load_store_sheet
from src.notifier import format_controller_summary, send_failure_alert, send_message
from src.reconcile import UnresolvedItemsError, run_reconciliation
from src.review import get_pending_reviews

REGISTER_PAGES = (
    ROOT / "kestrel-data" / "t4_register_page1.jpg",
    ROOT / "kestrel-data" / "t4_register_page2.jpg",
)
ERP_PATH = ROOT / "kestrel-data" / "t4_erp_grn.csv"
EXCEL_PATH = ROOT / "kestrel-data" / "t4_store_sheet.xlsx"
MONTH = "2026-09"
YARD_FACTOR = 0.9144
SUGGESTED_QTY = {"4419": 66.0, "4428": 250.0}
BUCKETS = (
    ("MATCHED", "Matched"),
    ("MATCHED_WITH_DUPLICATE", "Matched with Excel duplicate"),
    ("MISMATCHED", "Mismatched"),
    ("MISSING_FROM_SOURCE", "Missing from source"),
)
FIELD_LABELS = {
    "lot_no": "Lot",
    "date": "Date",
    "qty": "Qty",
    "uom_and_qty": "UOM/Qty",
    "supplier": "Supplier",
    "item_desc": "Item",
}
ERP_PART_LABELS = {
    "4417": "GRN 4417 quantity (ERP versus register)",
    "4421": "GRN 4421 missing from ERP",
    "4425": "GRN 4425 yard-to-meter numeric difference",
    "4430": "GRN 4430 ERP-only quantity",
}
EXCEL_PART_LABELS = {
    "4409_duplicate": "GRN 4409 duplicate Excel row",
    "4419": "GRN 4419 Excel still unamended",
    "return": "Excel row 31 torn return",
}


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _reviewer_name() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "reviewer"


def _clock_label(moment: datetime) -> str:
    return moment.strftime("%A, %d %b %Y — %H:%M PKT")


def _fmt(value: float) -> str:
    return f"{value:,.2f}"


def _signed(value: float) -> str:
    return f"{value:+,.2f}"


def _register_pointer(row: dict) -> str:
    return f"[Reg P{int(row['page_no'])}:R{int(row['row_no']):02d}]"


def _is_unclear(row: dict) -> bool:
    return bool(row.get("is_unclear"))


def _crop_path(row: dict) -> Path | None:
    raw = row.get("crop_path")
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        path = ROOT / path
    return path


def _init_rule_widgets() -> None:
    if "date_days" in st.session_state:
        return
    rules = load_rules()
    st.session_state.date_days = int(rules.tolerances.date_days)
    st.session_state.qty_pct = float(rules.tolerances.qty_pct)
    st.session_state.auto_convert = abs(rules.unit_conversions.yds_to_m - YARD_FACTOR) < 1e-9
    st.session_state.auto_net = bool(rules.behavior.auto_net_returns_by_lot)


def _require_inputs() -> None:
    missing = [path.name for path in (*REGISTER_PAGES, ERP_PATH, EXCEL_PATH) if not path.exists()]
    if missing:
        st.error("Missing input file: " + ", ".join(missing))
        st.stop()


def _ensure_register_loaded() -> None:
    for path in REGISTER_PAGES:
        digest = sha256_file(path)
        conn = init_db()
        try:
            cached = get_processed_page(conn, digest)
        finally:
            conn.close()
        if cached is None:
            extract_register_page(path, force=False)


def _register_rows() -> list[dict]:
    conn = init_db()
    try:
        return [dict(row) for row in list_official_register_rows(conn)]
    finally:
        conn.close()


def _page_meta(path, just_extracted: dict) -> dict:
    digest = sha256_file(path)
    conn = init_db()
    try:
        cached = get_processed_page(conn, digest)
    finally:
        conn.close()
    if just_extracted.get(path.name):
        status = "Extracted"
    elif cached is not None:
        status = "CACHE HIT — Skipped API"
    else:
        status = "Extracted"
    return {
        "digest": digest,
        "processed_at": cached["processed_at"] if cached is not None else "—",
        "row_count": int(cached["row_count"]) if cached is not None else 0,
        "status": status,
    }


def _list_resolutions() -> list[dict]:
    conn = init_db()
    try:
        rows = conn.execute(
            """
            SELECT grn_no, raw_ocr_value, resolved_value, resolved_by, resolved_at
            FROM human_resolutions
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _reset_doubtful_items() -> None:
    conn = init_db()
    try:
        conn.execute(
            """
            UPDATE register_rows
            SET resolved_qty = NULL,
                resolved_by = NULL,
                resolved_at = NULL
            WHERE grn_no IN ('4419', '4428')
            """
        )
        conn.commit()
    finally:
        conn.close()


def _save_rules(date_days: int, qty_pct: float, auto_convert: bool, auto_net: bool) -> None:
    rules = load_rules()
    payload = {
        "unit_conversions": {"yds_to_m": YARD_FACTOR if auto_convert else 1.0},
        "tolerances": {"date_days": int(date_days), "qty_pct": float(qty_pct)},
        "audit_rules": {
            "trusted_erp_users": list(rules.audit_rules.trusted_erp_users),
            "max_entry_delay_hours": int(rules.audit_rules.max_entry_delay_hours),
        },
        "behavior": {"auto_net_returns_by_lot": bool(auto_net)},
    }
    RULES_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _confirm_resolution(row: dict, qty: float) -> None:
    conn = init_db()
    try:
        record_human_resolution(
            conn,
            grn_no=str(row["grn_no"]),
            field_name=row.get("unclear_field") or "qty",
            raw_ocr_value=row.get("raw_qty_text"),
            resolved_value=f"{float(qty):.1f}",
            reviewer_note="Confirmed in Streamlit review",
            resolved_by=_reviewer_name(),
            resolved_at=get_scenario_now().isoformat(),
            page_no=int(row["page_no"]) if row.get("page_no") is not None else None,
        )
    finally:
        conn.close()


def _save_failed_run(exc: Exception) -> None:
    conn = init_db()
    try:
        save_reconciliation_run(
            conn,
            run_id=f"{MONTH}:failed:{uuid.uuid4().hex[:8]}",
            status="failed",
            summary_json=json.dumps({"error": str(exc)}),
            completeness_passed=False,
            run_time=get_scenario_now().isoformat(),
        )
    finally:
        conn.close()


def _capture(send) -> list[str]:
    # Runtime notifier logs under pipeline.notifier (src.notifier is only a shim).
    loggers = [
        logging.getLogger("pipeline.notifier"),
        logging.getLogger("src.notifier"),
    ]
    handler = _LogCapture()
    previous_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
    try:
        send()
    finally:
        for logger, previous in zip(loggers, previous_levels):
            logger.removeHandler(handler)
            logger.setLevel(previous)
    return handler.lines


def _source_counts(outcomes) -> dict[str, dict[str, int]]:
    counts = {status: {"Register": 0, "ERP": 0, "Store sheet": 0} for status, _label in BUCKETS}
    for outcome in outcomes:
        bucket = counts.get(outcome.status)
        if bucket is None:
            continue
        for pointer in outcome.pointers:
            if pointer.startswith("[Reg"):
                bucket["Register"] += 1
            elif pointer.startswith("[ERP"):
                bucket["ERP"] += 1
            elif pointer.startswith("[Excel"):
                bucket["Store sheet"] += 1
    return counts


def _phone_html(body: str, moment: datetime) -> str:
    safe = html.escape(body)
    stamp = html.escape(moment.strftime("%H:%M"))
    return f"""
    <div style="max-width:390px;margin:0 auto;background:#111;border-radius:28px;padding:14px 12px 18px;">
      <div style="height:18px;text-align:center;color:#d6d6d6;font-size:12px;font-family:sans-serif;">{stamp}</div>
      <div style="background:#075E54;color:#fff;font-family:sans-serif;font-weight:650;padding:10px 12px;border-radius:14px 14px 0 0;">
        Finance Controller
      </div>
      <pre style="margin:0;white-space:pre-wrap;word-wrap:break-word;background:#0b141a;color:#e9edef;font-family:Segoe UI,sans-serif;font-size:13.5px;line-height:1.45;padding:12px;border-radius:0 0 14px 14px;max-height:560px;overflow:auto;">{safe}</pre>
    </div>
    """


def _render_sidebar() -> None:
    moment = get_scenario_now()
    st.sidebar.markdown(
        f"""
        <div style="background:#10243f;color:#f7f4ef;padding:14px 14px 12px;border-radius:12px;">
          <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.7;">Scenario Now</div>
          <div style="font-size:15px;font-weight:650;margin-top:6px;line-height:1.35;">{html.escape(_clock_label(moment))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.caption("Loaded from SCENARIO_NOW. Asia/Karachi. The wall clock is not used.")
    st.sidebar.divider()
    st.sidebar.subheader("Demo controls")
    if st.sidebar.button("🔄 Reset Doubtful Items to Unresolved", width="stretch"):
        _reset_doubtful_items()
        st.session_state.flash = "GRN 4419 and GRN 4428 are unresolved again. The audit log was kept."
        st.rerun()
    st.sidebar.caption("Clears the confirmed quantity on those two rows so the review step can be recorded again.")
    if st.sidebar.button("⚡ Force Re-Extract Pages", width="stretch"):
        with st.spinner("Re-extracting register pages…"):
            for path in REGISTER_PAGES:
                extract_register_page(path, force=True)
        st.session_state.page_status = {path.name: "Extracted" for path in REGISTER_PAGES}
        st.session_state.flash = "Both register pages were re-extracted. Stored rows for those pages were replaced."
        st.rerun()
    st.sidebar.caption("Runs vision with force. A later load of the same page hash skips the API.")
    st.sidebar.divider()
    st.sidebar.subheader("Live requirement editor")
    _init_rule_widgets()
    date_days = st.sidebar.number_input("Date Tolerance (Days)", min_value=0, step=1, key="date_days")
    qty_pct = st.sidebar.number_input("Qty Tolerance (%)", min_value=0.0, step=0.1, format="%.1f", key="qty_pct")
    auto_convert = st.sidebar.checkbox("Auto-Convert Yards to Meters (1 yd = 0.9144 m)", key="auto_convert")
    auto_net = st.sidebar.checkbox("Auto-Net Supplier Returns by Lot No.", key="auto_net")
    st.sidebar.caption("Date tolerance 1 treats GRN 4415 (12 Sep vs 13 Sep) as a tolerated variance.")
    st.sidebar.caption("Yards on keeps 1200 yds equal to 1097.28 m. Off writes the factor as 1.0.")
    st.sidebar.caption("Auto-net folds the −5,000 pcs return on lot L2609-002 into GRN 4402.")
    if st.sidebar.button("Save & Apply Rules", type="primary", width="stretch"):
        _save_rules(int(date_days), float(qty_pct), bool(auto_convert), bool(auto_net))
        st.session_state.flash = "Rules saved. Reconciliation is using the updated file."
        st.rerun()
    st.sidebar.caption("Unsaved widget changes wait until this button. Trusted ERP users stay as they are.")


def _render_ocr(register_rows: list[dict], just_extracted: dict) -> None:
    st.subheader("Register pages")
    columns = st.columns(2)
    for column, path in zip(columns, REGISTER_PAGES):
        meta = _page_meta(path, just_extracted)
        with column:
            st.markdown(f"**{path.name}**")
            st.image(str(path), width="stretch")
            if meta["status"].startswith("CACHE"):
                st.success(meta["status"])
            else:
                st.info(meta["status"])
            st.caption(f"processed_at {meta['processed_at']} · {meta['row_count']} rows")
            st.code(meta["digest"], language=None)
    st.subheader(f"Extracted rows ({len(register_rows)})")
    if not register_rows:
        st.info("No register rows are stored yet.")
        return
    frame = pd.DataFrame(
        [
            {
                "Pointer": _register_pointer(row),
                "Date": row.get("raw_date") or "",
                "GRN": row.get("grn_no") or "",
                "Supplier": row.get("supplier") or "",
                "Item": row.get("item_desc") or "",
                "Lot": row.get("lot_no") or "",
                "Raw OCR": row.get("raw_qty_text") or "",
                "UOM": row.get("uom") or "",
                "Flag": "UNCLEAR" if _is_unclear(row) else "OK",
            }
            for row in register_rows
        ]
    )
    styled = frame.style.apply(
        lambda row: ["background-color: #fde8e8" if row["Flag"] == "UNCLEAR" else "" for _cell in row],
        axis=1,
    )
    st.dataframe(styled, width="stretch", hide_index=True)


def _render_review(register_rows: list[dict]) -> None:
    doubtful = [row for row in register_rows if _is_unclear(row)]
    crisp = len(register_rows) - len(doubtful)
    left, right = st.columns(2)
    left.metric("Crisp rows auto-accepted", crisp)
    right.metric("Doubtful items flagged", len(doubtful))
    st.caption(f"{crisp} Crisp Rows Auto-Accepted | {len(doubtful)} Doubtful Items Flagged for Human Review")
    if not doubtful:
        st.info("No doubtful rows are on the register pages.")
    for row in doubtful:
        grn = str(row.get("grn_no"))
        resolved = row.get("resolved_at") not in (None, "")
        with st.container(border=True):
            photo, detail = st.columns([1, 1.25])
            with photo:
                crop = _crop_path(row)
                if crop is not None and crop.exists():
                    st.image(str(crop), caption=crop.name, width="stretch")
                else:
                    st.warning("Row crop is not on disk yet. Force re-extract to regenerate it.")
            with detail:
                st.markdown(f"### GRN {grn}")
                st.markdown(
                    "\n".join(
                        [
                            f"**Date:** {row.get('raw_date') or '—'}",
                            f"**Supplier:** {row.get('supplier') or '—'}",
                            f"**Item:** {row.get('item_desc') or '—'}",
                            f"**Lot:** {row.get('lot_no') or '—'}",
                            f"**Raw OCR text:** {row.get('raw_qty_text') or '—'}",
                            f"**Unclear reason:** {row.get('unclear_reason') or '—'}",
                        ]
                    )
                )
                if resolved:
                    st.success(
                        f"Confirmed {row.get('resolved_qty')} by {row.get('resolved_by')} at {row.get('resolved_at')}."
                    )
                else:
                    qty_key = f"qty_{grn}"
                    if qty_key not in st.session_state:
                        st.session_state[qty_key] = float(SUGGESTED_QTY.get(grn, 0.0))
                    with st.form(f"resolve_{grn}"):
                        qty = st.number_input(
                            "Confirmed quantity",
                            min_value=0.0,
                            step=1.0,
                            key=qty_key,
                        )
                        st.caption("Nothing is saved until you press Confirm Resolution.")
                        submitted = st.form_submit_button("Confirm Resolution")
                    if submitted:
                        _confirm_resolution(row, float(qty))
                        st.session_state.flash = f"GRN {grn} confirmed as {_fmt(float(qty))}."
                        st.rerun()
    st.subheader("Audit log of human resolutions")
    resolutions = _list_resolutions()
    if not resolutions:
        st.info("No human resolutions recorded yet.")
        return
    st.dataframe(
        pd.DataFrame(resolutions)[
            ["grn_no", "raw_ocr_value", "resolved_value", "resolved_by", "resolved_at"]
        ],
        width="stretch",
        hide_index=True,
    )


def _missing_frame(outcomes) -> pd.DataFrame:
    rows = []
    for outcome in outcomes:
        if outcome.status != "MISSING_FROM_SOURCE":
            continue
        absent = []
        if outcome.reg is None:
            absent.append("Register")
        if outcome.erp is None:
            absent.append("ERP")
        if outcome.excel is None:
            absent.append("Store sheet")
        if outcome.entity_key == "4430":
            detail = (
                f"Entered by {outcome.entered_by} at {outcome.entered_at}. "
                f"{outcome.late_entry_reason or 'Late ERP entry.'}"
            )
        elif str(outcome.entity_key).startswith("RETURN") and outcome.excel is not None:
            detail = f"{outcome.excel.display}. Lot {outcome.excel.lot}. {outcome.remarks or ''}".strip()
        else:
            present = [
                f"{fact.pointer} {fact.display}"
                for fact in (outcome.reg, outcome.erp, outcome.excel)
                if fact is not None
            ]
            detail = "; ".join(present)
        rows.append(
            {
                "Entity": outcome.entity_key,
                "Absent from": ", ".join(absent) or "—",
                "Pointers": ", ".join(outcome.pointers),
                "Detail": detail,
            }
        )
    return pd.DataFrame(rows)


def _mismatch_frame(outcomes) -> pd.DataFrame:
    rows = []
    for outcome in outcomes:
        if outcome.status != "MISMATCHED":
            continue
        for break_ in outcome.fields:
            rows.append(
                {
                    "GRN": outcome.entity_key,
                    "Field": FIELD_LABELS.get(break_.field, break_.field),
                    "Register": break_.reg or "—",
                    "ERP": break_.erp or "—",
                    "Store sheet": break_.excel or "—",
                    "Pointers": ", ".join(outcome.pointers),
                }
            )
    return pd.DataFrame(rows)


def _bridge_frame(parts: dict[str, float], labels: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"Component": labels.get(key, key), "Amount": value}
            for key, value in parts.items()
        ]
    )


def _render_proof(pending, report, proof, pipeline_error, rules) -> None:
    if pending:
        names = ", ".join(f"GRN {row['grn_no']}" for row in pending)
        st.error(
            f"Final reconciliation is blocked. Resolve the doubtful register rows in the Human Review tab first: {names}."
        )
        return
    if pipeline_error is not None or report is None or proof is None:
        st.error(f"Completeness failed. The controller summary was not prepared. {pipeline_error}")
        return

    matched = sum(1 for outcome in report.outcomes if outcome.status == "MATCHED")
    duplicated = sum(1 for outcome in report.outcomes if outcome.status == "MATCHED_WITH_DUPLICATE")
    mismatched = sum(1 for outcome in report.outcomes if outcome.status == "MISMATCHED")
    missing = sum(1 for outcome in report.outcomes if outcome.status == "MISSING_FROM_SOURCE")
    one, two, three, four = st.columns(4)
    one.metric("Matched GRNs", f"{matched} + {duplicated} w/ Excel dup")
    two.metric("Mismatched GRNs", mismatched)
    three.metric("Missing from Source", missing)
    four.metric("Completeness Status", "100% Tied" if proof.passed else "Failed")
    yard = rules.unit_conversions.yds_to_m
    netting = "on" if rules.behavior.auto_net_returns_by_lot else "off"
    st.caption(
        f"Rules in force: date tolerance {rules.tolerances.date_days} day(s) · "
        f"qty tolerance {rules.tolerances.qty_pct:g}% · yard factor {yard:g} · auto-net {netting}."
    )

    st.subheader("Exceptions first — missing from a source")
    missing_frame = _missing_frame(report.outcomes)
    if missing_frame.empty:
        st.info("No source is missing a row under the current rules.")
    else:
        st.dataframe(missing_frame, width="stretch", hide_index=True)

    st.subheader("Field-level mismatches")
    mismatch_frame = _mismatch_frame(report.outcomes)
    if mismatch_frame.empty:
        st.info("No field mismatches under the current rules.")
    else:
        st.dataframe(mismatch_frame, width="stretch", hide_index=True)

    for outcome in report.outcomes:
        if outcome.status != "MATCHED_WITH_DUPLICATE":
            continue
        extra = ", ".join(fact.pointer for fact in outcome.duplicate_excel)
        shown = ", ".join(fact.display for fact in outcome.duplicate_excel)
        st.warning(
            f"GRN {outcome.entity_key}: primary row matches all three sources. "
            f"Duplicate Excel row {extra} ({shown}) is isolated so the raw total still includes it."
        )
    if report.hygiene_warnings:
        st.markdown("**Data-format warnings**")
        for warning in report.hygiene_warnings:
            st.warning(warning)
    tolerated = [
        (outcome.entity_key, warning)
        for outcome in report.outcomes
        for warning in outcome.warnings
        if str(warning).startswith("date within")
    ]
    if tolerated:
        st.markdown("**Tolerated variances**")
        for entity_key, warning in tolerated:
            st.info(f"GRN {entity_key}: {warning}")

    st.subheader("Mathematical proof of completeness")
    st.markdown("**Row partition**")
    counts = _source_counts(report.outcomes)
    partition = pd.DataFrame(
        [
            {"Bucket": label, **counts[status]}
            for status, label in BUCKETS
        ]
    )
    totals = {
        "Bucket": "Total",
        "Register": int(partition["Register"].sum()),
        "ERP": int(partition["ERP"].sum()),
        "Store sheet": int(partition["Store sheet"].sum()),
    }
    partition = pd.concat([partition, pd.DataFrame([totals])], ignore_index=True)
    st.dataframe(partition, width="stretch", hide_index=True)
    tied = (
        totals["Register"] == proof.register_rows
        and totals["ERP"] == proof.erp_rows
        and totals["Store sheet"] == proof.excel_rows
    )
    if tied:
        st.success(
            f"Register {totals['Register']}/{proof.register_rows} · "
            f"ERP {totals['ERP']}/{proof.erp_rows} · "
            f"Store sheet {totals['Store sheet']}/{proof.excel_rows}. "
            "Each source row is in exactly one bucket."
        )
    else:
        st.error("Partition totals do not match the raw source counts.")

    st.markdown("**Quantity sum variance bridge**")
    st.markdown(
        "\n".join(
            [
                f"Register resolved **{_fmt(proof.register_resolved_sum)}** "
                f"(legible raw sum {_fmt(proof.register_raw_sum)}).",
                f"ERP **{_fmt(proof.erp_sum)}**, difference **{_signed(proof.erp_bridge)}**.",
                f"Store sheet **{_fmt(proof.excel_sum)}**, difference **{_signed(proof.excel_bridge)}**.",
            ]
        )
    )
    erp_col, excel_col = st.columns(2)
    number = st.column_config.NumberColumn(format="%+,.2f")
    with erp_col:
        st.markdown("ERP bridge")
        st.dataframe(
            _bridge_frame(proof.erp_bridge_parts, ERP_PART_LABELS),
            width="stretch",
            hide_index=True,
            column_config={"Amount": number},
        )
    with excel_col:
        st.markdown("Store sheet bridge")
        st.dataframe(
            _bridge_frame(proof.excel_bridge_parts, EXCEL_PART_LABELS),
            width="stretch",
            hide_index=True,
            column_config={"Amount": number},
        )
    st.markdown("**Per-UOM tie-out**")
    uom = pd.DataFrame(
        [
            {
                "UOM": tie.uom,
                "Register": tie.register_qty,
                "ERP": tie.erp_qty,
                "Store sheet": tie.excel_qty,
            }
            for tie in proof.uom_tie_out
        ]
    )
    qty_format = st.column_config.NumberColumn(format="%,.2f")
    st.dataframe(
        uom,
        width="stretch",
        hide_index=True,
        column_config={"Register": qty_format, "ERP": qty_format, "Store sheet": qty_format},
    )


def _render_message(summary: str | None, moment: datetime, blocked: str | None, pipeline) -> None:
    preview, actions = st.columns([1.15, 1])
    with preview:
        if summary:
            st.markdown(_phone_html(summary, moment), unsafe_allow_html=True)
        else:
            st.error(blocked or "The controller summary is not available.")
    with actions:
        st.subheader("Deliver")
        if summary and st.button("📤 Send Summary to Controller", type="primary", width="stretch"):
            with st.spinner("Sending the controller summary…"):
                # force_send=True so Streamlit re-clicks actually hit WhatsApp again
                # (idempotency still protects unattended CLI re-runs without the flag).
                lines = _capture(
                    lambda: send_message(summary, message_type="SUMMARY", force_send=True)
                )
            cloud_ok = any("WhatsApp Cloud accepted" in line for line in lines)
            cloud_fail = any(
                "whatsapp_cloud delivery failed" in line.lower()
                or "WhatsApp Cloud" in line and "failed" in line.lower()
                for line in lines
            )
            st.session_state.summary_notice = {
                "skipped": any("IDEMPOTENT SKIP" in line for line in lines),
                "cloud_ok": cloud_ok,
                "cloud_fail": cloud_fail,
                "lines": lines,
            }
        if st.button("🚨 Simulate Pipeline Failure (--demo-failure)", width="stretch"):
            with st.spinner("Running the safe-failure path…"):
                notice = _simulate_failure(*pipeline)
            st.session_state.alert_notice = notice
        summary_notice = st.session_state.get("summary_notice")
        if summary_notice:
            if summary_notice.get("cloud_ok"):
                st.success(
                    "WhatsApp Cloud accepted the summary for "
                    f"+{os.environ.get('TEST_RECIPIENT_WHATSAPP', 'your test recipient')}. "
                    "Check the chat with the Meta test number (+1 555 152 9281)."
                )
            elif summary_notice.get("cloud_fail"):
                joined = "\n".join(summary_notice.get("lines") or [])
                if "code 190" in joined or "Authentication Error" in joined or "WHATSAPP_ACCESS_TOKEN" in joined:
                    st.error(
                        "WhatsApp Cloud rejected the access token (expired or invalid). "
                        "In Meta Developer Console → WhatsApp → API Setup, generate a new "
                        "temporary token, put it in `.env` as `WHATSAPP_ACCESS_TOKEN`, restart "
                        "Streamlit, then send again. Outbox still has a copy."
                    )
                else:
                    st.error("WhatsApp Cloud send failed. See log lines below; outbox still has a copy.")
            elif summary_notice["skipped"]:
                st.warning("[IDEMPOTENT SKIP] Duplicate message blocked")
            else:
                st.success("Summary written to outbox. Check WhatsApp if Cloud is enabled.")
            st.caption("Outbox copy: outbox/latest_summary.txt. Only the configured test recipient is used.")
            if summary_notice.get("lines"):
                with st.expander("Delivery log"):
                    st.code("\n".join(summary_notice["lines"]) or "(no log lines)")
        alert_notice = st.session_state.get("alert_notice")
        if alert_notice:
            st.error("Pipeline halted. The controller summary was not sent.")
            st.markdown("**[CRITICAL ALERT]** dispatched instead:")
            st.code(alert_notice["text"])
            if alert_notice["skipped"]:
                st.warning("[IDEMPOTENT SKIP] Duplicate message blocked")


def _simulate_failure(register_rows, erp_rows, store_rows, rules) -> dict:
    try:
        report = run_reconciliation(register_rows, erp_rows, store_rows, rules, month=MONTH)
        verify_completeness(report, register_rows[:-1], erp_rows, store_rows)
        exc: Exception = CompletenessError("Demo failure did not break completeness.")
    except (UnresolvedItemsError, CompletenessError) as caught:
        exc = caught
    _save_failed_run(exc)
    lines = _capture(lambda: send_failure_alert(str(exc)))
    alert_path = ROOT / "outbox" / "latest_alert.txt"
    text = alert_path.read_text(encoding="utf-8") if alert_path.exists() else str(exc)
    return {
        "text": text,
        "skipped": any("IDEMPOTENT SKIP" in line for line in lines),
        "lines": lines,
    }


def _load_pipeline():
    rules = load_rules()
    register_rows = _register_rows()
    erp_rows = load_erp_grn(ERP_PATH, rules)
    store_rows = load_store_sheet(EXCEL_PATH)
    pending = get_pending_reviews()
    report = None
    proof = None
    pipeline_error = None
    if not pending:
        try:
            report = run_reconciliation(register_rows, erp_rows, store_rows, rules, month=MONTH)
            proof = verify_completeness(report, register_rows, erp_rows, store_rows)
        except (UnresolvedItemsError, CompletenessError) as exc:
            pipeline_error = exc
    return rules, register_rows, erp_rows, store_rows, pending, report, proof, pipeline_error


UPLOAD_DIR = ROOT / "data" / "uploads"


def _safe_upload_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in Path(name).name)
    return cleaned or "register.jpg"


def _load_upload_rows(pages: list[int]) -> list[dict]:
    if not pages:
        return []
    conn = init_db()
    try:
        rows: list[dict] = []
        for page_no in pages:
            rows.extend(dict(row) for row in list_register_rows(conn, int(page_no)))
        return rows
    finally:
        conn.close()


def _upload_pending(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("is_unclear") and not row.get("resolved_at")
    ]


def _upload_message(rows: list[dict], outcomes) -> str:
    moment = get_scenario_now().strftime("%d %b %Y %H:%M PKT")
    lines = [
        "KESTREL extra register compare",
        moment,
        "Not the September 100% proof. Uploaded pages only.",
        "",
    ]
    exceptions = [item for item in outcomes if item.status != "MATCHED"]
    if not exceptions:
        lines.append("No exceptions on the uploaded GRNs.")
    else:
        lines.append("Exceptions on the uploaded pages:")
        for item in exceptions:
            detail = item.entity_key
            if item.fields:
                breaks = ", ".join(
                    f"{break_.field}: reg {break_.reg} | erp {break_.erp} | excel {break_.excel}"
                    for break_ in item.fields
                )
                detail = f"{detail} {breaks}"
            elif item.status == "MISSING_FROM_SOURCE":
                present = []
                if item.reg:
                    present.append(item.reg.pointer)
                if item.erp:
                    present.append(item.erp.pointer)
                if item.excel:
                    present.append(item.excel.pointer)
                detail = f"{detail} missing from a source ({', '.join(present)})"
            lines.append(f"- {item.status} {detail}")
    lines.append("")
    lines.append(f"Uploaded rows: {len(rows)}")
    return "\n".join(lines)


def _render_uploads(rules, erp_rows, store_rows) -> None:
    st.subheader("Extra register photos")
    st.caption(
        "These photos are read with Gemini and compared to the same ERP file and store sheet. "
        "They do not change the September 28/28/30 proof."
    )
    uploads = st.file_uploader(
        "Register photos",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
    )
    if st.button("Extract and compare", type="primary", disabled=not uploads):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        pages: list[int] = []
        errors: list[str] = []
        for index, uploaded in enumerate(uploads):
            dest = UPLOAD_DIR / f"{index + 1:02d}_{_safe_upload_name(uploaded.name)}"
            dest.write_bytes(uploaded.getvalue())
            page_no = 101 + index
            try:
                extract_register_image(dest, page_no, force=True)
                pages.append(page_no)
            except Exception as exc:
                errors.append(f"{uploaded.name}: {exc}")
        st.session_state["upload_pages"] = pages
        st.session_state["upload_errors"] = errors
        st.rerun()

    for message in st.session_state.get("upload_errors") or []:
        st.error(message)

    pages = [int(page) for page in st.session_state.get("upload_pages") or []]
    rows = _load_upload_rows(pages)
    if not rows:
        st.info("Upload one or more register photos, then extract and compare.")
        return

    table = pd.DataFrame(
        [
            {
                "Page": row.get("page_no"),
                "Row": row.get("row_no"),
                "GRN": row.get("grn_no"),
                "Date": row.get("raw_date"),
                "Supplier": row.get("supplier"),
                "Item": row.get("item_desc"),
                "Lot": row.get("lot_no"),
                "Qty": row.get("resolved_qty") if row.get("is_unclear") else row.get("parsed_qty"),
                "Raw qty": row.get("raw_qty_text"),
                "UOM": row.get("uom"),
                "Unclear": "Yes" if row.get("is_unclear") else "",
            }
            for row in rows
        ]
    )
    st.dataframe(table, width="stretch", hide_index=True)

    pending = _upload_pending(rows)
    if pending:
        st.warning("Confirm each unclear quantity before this compare runs.")
        for row in pending:
            grn = str(row.get("grn_no"))
            st.markdown(f"**GRN {grn}** · page {row.get('page_no')} row {row.get('row_no')}")
            if row.get("unclear_reason"):
                st.caption(str(row["unclear_reason"]))
            crop = row.get("crop_path")
            if crop and Path(str(crop)).exists():
                st.image(str(crop), caption=f"GRN {grn}")
            qty_key = f"upload_qty_{row.get('page_no')}_{grn}"
            if qty_key not in st.session_state:
                suggested = SUGGESTED_QTY.get(grn)
                if suggested is not None:
                    st.session_state[qty_key] = float(suggested)
            confirmed = st.number_input("Confirmed qty", min_value=0.0, step=1.0, key=qty_key)
            if st.button(f"Confirm GRN {grn}", key=f"upload_confirm_{row.get('page_no')}_{grn}"):
                _confirm_resolution(row, float(confirmed))
                st.rerun()
        return

    try:
        report = run_reconciliation(rows, erp_rows, store_rows, rules, month=MONTH)
    except UnresolvedItemsError as exc:
        st.error(str(exc))
        return

    uploaded_keys = {str(row.get("grn_no")) for row in rows}
    relevant = [
        item
        for item in report.outcomes
        if item.reg is not None or item.entity_key in uploaded_keys
    ]
    omitted = len(report.outcomes) - len(relevant)
    st.caption(
        f"Showing {len(relevant)} outcomes that touch an uploaded GRN. "
        f"{omitted} other ERP or store-sheet rows are the September book and are not listed. "
        "This is an extra compare, not the September 100% proof."
    )
    for status, label in BUCKETS:
        bucket = [item for item in relevant if item.status == status]
        st.markdown(f"**{label}** · {len(bucket)}")
        if not bucket:
            continue
        frame = pd.DataFrame(
            [
                {
                    "GRN": item.entity_key,
                    "Status": item.status,
                    "Pointers": ", ".join(item.pointers),
                    "Breaks": "; ".join(
                        f"{break_.field}: reg {break_.reg} | erp {break_.erp} | excel {break_.excel}"
                        for break_ in item.fields
                    ),
                }
                for item in bucket
            ]
        )
        st.dataframe(frame, width="stretch", hide_index=True)

    text = _upload_message(rows, relevant)
    st.text_area("Message", text, height=220)
    if st.button("Send these exceptions"):
        lines = _capture(lambda: send_message(text, message_type="UPLOAD", force_send=True))
        if any("accepted" in line.lower() for line in lines):
            st.success("WhatsApp accepted this extra compare.")
        else:
            st.info("Saved to the outbox. Check the delivery log if WhatsApp did not accept it.")
        with st.expander("Delivery log"):
            st.code("\n".join(lines) or "(no log lines)")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    st.set_page_config(
        page_title="Kestrel Receiving Reconciliation",
        page_icon="📋",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    just_extracted = st.session_state.pop("page_status", {})
    flash = st.session_state.pop("flash", None)
    _render_sidebar()
    st.title("Kestrel Receiving Reconciliation")
    st.caption("September 2026 · Register, ERP, and store sheet · Exceptions first")
    if flash:
        st.success(flash)
    _require_inputs()
    try:
        _ensure_register_loaded()
        rules, register_rows, erp_rows, store_rows, pending, report, proof, pipeline_error = _load_pipeline()
    except Exception as exc:
        st.error(f"Input load failed. Nothing was sent. {exc}")
        st.stop()
    moment = get_scenario_now()
    summary = None
    if report is not None and proof is not None:
        summary = format_controller_summary(report, proof, moment, month=MONTH)
    if pending:
        blocked = "Final reconciliation is blocked until every doubtful row is confirmed."
    elif pipeline_error is not None:
        blocked = str(pipeline_error)
    else:
        blocked = None

    ocr_tab, review_tab, proof_tab, message_tab, upload_tab = st.tabs(
        [
            "📄 Register OCR & Incremental Page Gate",
            "🔍 Human Review Queue (Doubtful Items Only)",
            "⚖️ 3-Way Reconciliation & Proof of Completeness",
            "📱 Controller WhatsApp Summary & Safe Failure Demo",
            "📤 Upload extra register pages",
        ]
    )
    with ocr_tab:
        _render_ocr(register_rows, just_extracted)
    with review_tab:
        _render_review(register_rows)
    with proof_tab:
        _render_proof(pending, report, proof, pipeline_error, rules)
    with message_tab:
        _render_message(
            summary,
            moment,
            blocked,
            (register_rows, erp_rows, store_rows, rules),
        )
    with upload_tab:
        _render_uploads(rules, erp_rows, store_rows)


if __name__ == "__main__":
    main()
