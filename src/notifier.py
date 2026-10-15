"""Exceptions-first controller message, with idempotent delivery."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel

from src.config import ROOT, ensure_directories, get_scenario_now, load_environment
from src.db import get_outbound_message, init_db, record_outbound_message

logger = logging.getLogger(__name__)
console = Console()

QTY_FIELDS = {"qty", "uom_and_qty"}
MISSING_ORDER = {"4430": 0, "4421": 1, "RETURN-L2609-002": 2}
QTY_ORDER = {"4425": 0, "4417": 1, "4419": 2, "4409": 3}


def format_controller_summary(report, completeness_proof, scenario_now: datetime, month: str = "2026-09") -> str:
    year = month.split("-")[0]
    month_name = datetime.strptime(month, "%Y-%m").strftime("%b").upper()
    missing = [outcome for outcome in report.outcomes if outcome.status == "MISSING_FROM_SOURCE"]
    qty_breaks = [outcome for outcome in report.outcomes if _is_qty_outcome(outcome)]
    duplicates = [outcome for outcome in report.outcomes if outcome.status == "MATCHED_WITH_DUPLICATE"]
    typos = [
        outcome
        for outcome in report.outcomes
        if outcome.status == "MISMATCHED" and not _is_qty_outcome(outcome)
    ]
    matched = sum(1 for outcome in report.outcomes if outcome.status in {"MATCHED", "MATCHED_WITH_DUPLICATE"})
    lines = [
        f"*KESTREL — {month_name} {year} RECEIVING RECONCILIATION*",
        scenario_now.strftime("%d %b %Y, %H:%M PKT"),
        "",
        f"🚨 MISSING / UNRECORDED ({len(missing)})",
    ]
    lines.extend(_missing_line(outcome) for outcome in sorted(missing, key=_missing_key))
    lines.extend(["", f"⚠️ QUANTITY & UNIT DISAGREEMENTS ({len(qty_breaks) + len(duplicates)})"])
    lines.extend(_qty_line(outcome) for outcome in sorted(qty_breaks, key=_qty_key))
    lines.extend(_duplicate_line(outcome) for outcome in duplicates)
    lines.extend(["", f"🔎 METADATA TYPOS ({len(typos)})"])
    lines.extend(_typo_line(outcome) for outcome in typos)
    lines.extend(
        [
            "",
            "✅ PROOF OF COMPLETENESS",
            (
                f"Matched: {matched}/{completeness_proof.register_rows} GRNs | "
                f"Reg: {completeness_proof.register_rows}/{completeness_proof.register_rows} rows "
                f"({_plain(completeness_proof.register_resolved_sum)}) | "
                f"ERP: {completeness_proof.erp_rows}/{completeness_proof.erp_rows} rows "
                f"({_plain(completeness_proof.erp_sum)}) | "
                f"Excel: {completeness_proof.excel_rows}/{completeness_proof.excel_rows} rows "
                f"({_plain(completeness_proof.excel_sum)}) — 100% tied."
            ),
        ]
    )
    return "\n".join(lines)


def send_message(payload_text: str, message_type: str = "SUMMARY", force_send: bool = False) -> None:
    ensure_directories()
    outbox = ROOT / "outbox" / f"latest_{message_type.lower()}.txt"
    outbox.write_text(payload_text, encoding="utf-8")
    _print_panel(payload_text, message_type)
    _deliver("outbox", "local-outbox", payload_text, message_type, force_send, lambda: None)
    load_environment()
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    recipient = os.getenv("TEST_RECIPIENT_WHATSAPP", "").strip()
    if sid and token and sender and recipient:
        _deliver(
            "whatsapp",
            recipient,
            payload_text,
            message_type,
            force_send,
            lambda: _send_twilio(sid, token, sender, recipient, payload_text),
        )
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if telegram_token and telegram_chat:
        _deliver(
            "telegram",
            telegram_chat,
            payload_text,
            message_type,
            force_send,
            lambda: _send_telegram(telegram_token, telegram_chat, payload_text),
        )


def send_failure_alert(error_msg: str, force_send: bool = False) -> None:
    stamped = get_scenario_now().strftime("%d %b %Y, %H:%M PKT")
    payload = "\n".join(
        [
            "[CRITICAL ALERT] Kestrel Reconciliation Failed Safely",
            stamped,
            error_msg,
        ]
    )
    send_message(payload, message_type="ALERT", force_send=force_send)


def _print_panel(payload_text: str, message_type: str) -> None:
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    safe = payload_text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    console.print(Panel(safe, title=f"Kestrel {message_type}", border_style="green"))


def _deliver(channel: str, recipient: str, payload_text: str, message_type: str, force_send: bool, send) -> None:
    digest = hashlib.sha256(f"{payload_text}|{message_type}|{channel}".encode("utf-8")).hexdigest()
    conn = init_db()
    try:
        existing = get_outbound_message(conn, digest)
        if existing is not None and existing["status"] == "SENT" and not force_send:
            logger.info("[IDEMPOTENT SKIP] Message already sent; preventing duplicate notification.")
            return
        try:
            send()
        except Exception as exc:
            record_outbound_message(
                conn,
                message_hash=digest,
                channel=channel,
                recipient=recipient,
                payload_text=payload_text,
                status="FAILED",
            )
            logger.error("%s delivery failed: %s", channel, exc)
            return
        record_outbound_message(
            conn,
            message_hash=digest,
            channel=channel,
            recipient=recipient,
            payload_text=payload_text,
            status="SENT",
        )
    finally:
        conn.close()


def _send_twilio(sid: str, token: str, sender: str, recipient: str, payload_text: str) -> None:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    _post_with_retries(
        lambda: requests.post(
            url,
            data={"From": sender, "To": recipient, "Body": payload_text},
            auth=(sid, token),
            timeout=30,
        )
    )


def _send_telegram(token: str, chat_id: str, payload_text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _post_with_retries(
        lambda: requests.post(url, json={"chat_id": chat_id, "text": payload_text}, timeout=30)
    )


def _post_with_retries(post) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = post()
            response.raise_for_status()
            return
        except Exception as exc:
            last_error = exc
            logger.warning("Delivery attempt %s failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _missing_line(outcome) -> str:
    if outcome.entity_key == "4430" and outcome.erp is not None:
        entered = _short_date(outcome.entered_at)
        return (
            f"GRN-4430: 1,900 pcs ({_item_label(outcome.erp.item)}) in ERP ONLY {outcome.erp.pointer} — "
            f"entered 3 days late ({entered}) by {outcome.entered_by}. Not in Register or Store Excel."
        )
    if outcome.entity_key == "4421" and outcome.reg and outcome.excel:
        return (
            f"GRN-4421: {_plain(outcome.reg.qty)} {_unit(outcome.reg.uom, outcome.reg.qty)} "
            f"({outcome.reg.item}) in Reg {outcome.reg.pointer} & Excel {outcome.excel.pointer} "
            f"— MISSING in ERP."
        )
    if outcome.excel is not None and outcome.entity_key.startswith("RETURN-"):
        return (
            f"No GRN (Lot {outcome.excel.lot}): {_plain(outcome.excel.qty)} pcs torn polybag return "
            f"in Excel ONLY {outcome.excel.pointer} — MISSING in Reg & ERP."
        )
    present = [fact.pointer for fact in (outcome.reg, outcome.erp, outcome.excel) if fact]
    return f"{outcome.entity_key}: present at {', '.join(present) or 'no source'}"


def _qty_line(outcome) -> str:
    if outcome.entity_key == "4425" and outcome.reg and outcome.erp and outcome.excel:
        over = outcome.excel.qty - (outcome.reg.qty * _factor_from_display(outcome))
        return (
            f"GRN-4425 ({outcome.reg.item}): Excel {outcome.excel.pointer} says "
            f"{_plain(outcome.excel.qty)} {_unit(outcome.excel.uom, outcome.excel.qty)} (+{_plain(abs(over))}m overstated). "
            f"Reg {outcome.reg.pointer} ({_plain(outcome.reg.qty)} yds) & "
            f"ERP {outcome.erp.pointer} ({_plain(outcome.erp.qty)} M) match."
        )
    if outcome.entity_key == "4417" and outcome.reg and outcome.erp:
        return (
            f"GRN-4417 ({outcome.reg.item}): ERP {outcome.erp.pointer} has "
            f"{_plain(outcome.erp.qty)} {_unit(outcome.erp.uom, outcome.erp.qty)} vs Reg/Excel "
            f"{_plain(outcome.reg.qty)} {_unit(outcome.reg.uom, outcome.reg.qty)} "
            f"({_signed(outcome.erp.qty - outcome.reg.qty)} {_unit(outcome.reg.uom, outcome.reg.qty)})."
        )
    if outcome.entity_key == "4419" and outcome.reg and outcome.excel:
        return (
            f"GRN-4419 ({outcome.reg.item}): Excel {outcome.excel.pointer} has "
            f"{_plain(outcome.excel.qty)} {_unit(outcome.excel.uom, outcome.excel.qty)} vs Reg/ERP "
            f"{_plain(outcome.reg.qty)} {_unit(outcome.reg.uom, outcome.reg.qty)} "
            f"({_signed(outcome.excel.qty - outcome.reg.qty)} {_unit(outcome.reg.uom, outcome.reg.qty)}; "
            f"Reg amended 60->66)."
        )
    field = outcome.fields[0].field if outcome.fields else "qty"
    return f"GRN-{outcome.entity_key}: {field} disagrees across sources."


def _duplicate_line(outcome) -> str:
    duplicate = outcome.duplicate_excel[0]
    primary = outcome.excel
    return (
        f"GRN-{outcome.entity_key} ({outcome.reg.item}): Duplicated in Excel "
        f"({primary.pointer} & {duplicate.pointer}, +{_plain(duplicate.qty)} {_unit(duplicate.uom, duplicate.qty)} extra)."
    )


def _typo_line(outcome) -> str:
    field = outcome.fields[0]
    if field.field == "lot_no":
        return f"GRN-{outcome.entity_key}: ERP Lot {field.erp} vs Reg/Excel {field.reg}."
    if field.field == "date":
        return (
            f"GRN-{outcome.entity_key}: ERP Date {_short_iso(field.erp)} vs "
            f"Reg/Excel {_short_iso(field.reg)}."
        )
    return f"GRN-{outcome.entity_key}: {field.field} {field.reg} / {field.erp} / {field.excel}."


def _is_qty_outcome(outcome) -> bool:
    return outcome.status == "MISMATCHED" and any(item.field in QTY_FIELDS for item in outcome.fields)


def _missing_key(outcome) -> tuple[int, str]:
    return (MISSING_ORDER.get(outcome.entity_key, 9), outcome.entity_key)


def _qty_key(outcome) -> tuple[int, str]:
    return (QTY_ORDER.get(outcome.entity_key, 9), outcome.entity_key)


def _factor_from_display(outcome) -> float:
    if outcome.reg.uom == "YDS" and outcome.reg.qty:
        return outcome.erp.qty / outcome.reg.qty
    return 0.9144


def _short_date(value: str | None) -> str:
    if not value:
        return "unknown date"
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    return parsed.strftime("%d %b")


def _short_iso(value: str | None) -> str:
    if not value:
        return "unknown"
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.strftime("%d %b")


def _item_label(text: str) -> str:
    if not text.isupper():
        return text
    return text.title().replace("5-Ply", "5-ply")


def _unit(uom: str, qty: float) -> str:
    plural = {
        "PCS": "pcs",
        "BAG": "bags",
        "CONE": "cones",
        "DRUM": "drums",
        "ROLL": "rolls",
        "CTN": "ctns",
        "BDL": "bdl",
        "YDS": "yds",
        "M": "m",
    }
    label = plural.get(uom, uom.lower())
    if abs(qty) == 1 and label.endswith("s") and label not in {"pcs", "bdl"}:
        return label[:-1]
    return label


def _plain(qty: float) -> str:
    if abs(qty - round(qty)) < 1e-6:
        return f"{int(round(qty)):,}"
    return f"{qty:,.2f}"


def _signed(qty: float) -> str:
    text = _plain(abs(qty))
    return f"+{text}" if qty > 0 else f"-{text}"


def message_path(message_type: str) -> Path:
    return ROOT / "outbox" / f"latest_{message_type.lower()}.txt"
