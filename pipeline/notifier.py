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

from pipeline.config import ROOT, ensure_directories, get_scenario_now, load_environment
from pipeline.db import get_outbound_message, init_db, record_outbound_message

logger = logging.getLogger(__name__)
console = Console()

QTY_FIELDS = {"qty", "uom_and_qty"}
MISSING_ORDER = {"4430": 0, "4421": 1, "RETURN-L2609-002": 2}
QTY_ORDER = {"4425": 0, "4417": 1, "4419": 2, "4409": 3}


def format_controller_summary(report, completeness_proof, scenario_now: datetime, month: str = "2026-09") -> str:
    year = month.split("-")[0]
    month_name = datetime.strptime(month, "%Y-%m").strftime("%b")
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
        f"Kestrel {month_name} {year} receiving",
        scenario_now.strftime("%d %b %Y, %H:%M PKT"),
    ]
    _append_section(lines, "Missing", [_missing_line(item) for item in sorted(missing, key=_missing_key)])
    _append_section(
        lines,
        "Quantity",
        [_qty_line(item) for item in sorted(qty_breaks, key=_qty_key)]
        + [_duplicate_line(item) for item in duplicates],
    )
    _append_section(lines, "Typos", [_typo_line(item) for item in typos])
    lines.extend(
        [
            "",
            (
                f"Reg: {completeness_proof.register_rows}/{completeness_proof.register_rows} | "
                f"ERP: {completeness_proof.erp_rows}/{completeness_proof.erp_rows} | "
                f"Excel: {completeness_proof.excel_rows}/{completeness_proof.excel_rows} — 100% tied"
            ),
            (
                f"{matched} matched. "
                f"Reg {_plain(completeness_proof.register_resolved_sum)} · "
                f"ERP {_plain(completeness_proof.erp_sum)} · "
                f"Excel {_plain(completeness_proof.excel_sum)}"
            ),
        ]
    )
    return "\n".join(lines)


def _append_section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines.extend(["", f"{title} ({len(items)})"])
    lines.extend(items)


def send_message(payload_text: str, message_type: str = "SUMMARY", force_send: bool = False) -> None:
    ensure_directories()
    outbox = ROOT / "outbox" / f"latest_{message_type.lower()}.txt"
    outbox.write_text(payload_text, encoding="utf-8")
    _print_panel(payload_text, message_type)
    _deliver("outbox", "local-outbox", payload_text, message_type, force_send, lambda: None)
    load_environment()
    recipient = os.getenv("TEST_RECIPIENT_WHATSAPP", "").strip()
    cloud_enabled = os.getenv("WHATSAPP_CLOUD_SENDING_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cloud_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    if cloud_enabled and cloud_token and phone_number_id and recipient:
        cloud_to = _digits_only(recipient)
        _deliver(
            "whatsapp_cloud",
            cloud_to,
            payload_text,
            message_type,
            force_send,
            lambda: _send_whatsapp_cloud(cloud_token, phone_number_id, cloud_to, payload_text),
        )
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    if sid and token and sender and recipient:
        twilio_to = _twilio_whatsapp_address(recipient)
        _deliver(
            "whatsapp",
            twilio_to,
            payload_text,
            message_type,
            force_send,
            lambda: _send_twilio(sid, token, sender, twilio_to, payload_text),
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
            detail = str(exc)
            if channel == "whatsapp_cloud" and ("code\":190" in detail or "Authentication Error" in detail):
                logger.error(
                    "whatsapp_cloud delivery failed: Meta rejected WHATSAPP_ACCESS_TOKEN "
                    "(HTTP 401 / code 190). Temporary tokens expire in about 24 hours. "
                    "Generate a new token in Meta Developer Console → WhatsApp → API Setup, "
                    "put it in .env as WHATSAPP_ACCESS_TOKEN, then send again."
                )
            else:
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


def _send_whatsapp_cloud(
    access_token: str, phone_number_id: str, recipient: str, payload_text: str
) -> None:
    """Send via Meta WhatsApp Cloud API (Graph).

    Free-form text only delivers inside the 24h customer-care window (the user must
    message the Meta test/business number first). Hello World templates do NOT open
    that window. Optional WHATSAPP_TEMPLATE_NAME sends an approved template with the
    summary packed into body variable {{1}}.
    """
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0").strip() or "v21.0"
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    plain = _whatsapp_plain_text(payload_text)
    body = plain if len(plain) <= 4000 else plain[:3990] + "\n..."
    template_name = os.getenv("WHATSAPP_TEMPLATE_NAME", "").strip()
    template_lang = os.getenv("WHATSAPP_TEMPLATE_LANG", "en_US").strip() or "en_US"

    if template_name:
        # Template body variables cannot contain raw newlines.
        packed = " | ".join(line.strip() for line in body.splitlines() if line.strip())
        if len(packed) > 1000:
            packed = packed[:990] + "..."
        response_data = _post_with_retries(
            lambda: requests.post(
                url,
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"code": template_lang},
                        "components": [
                            {
                                "type": "body",
                                "parameters": [{"type": "text", "text": packed}],
                            }
                        ],
                    },
                },
                timeout=30,
            )
        )
        _log_whatsapp_accept(recipient, response_data, kind="template")
        return

    response_data = _post_with_retries(
        lambda: requests.post(
            url,
            headers=headers,
            json={
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
            timeout=30,
        )
    )
    _log_whatsapp_accept(recipient, response_data, kind="text")
    console.print(
        "[yellow]Note: Meta only delivers free-form text after YOU message the "
        "test number (+1 555…) first. Hello World alone is not enough.[/yellow]"
    )


def _whatsapp_plain_text(payload_text: str) -> str:
    """Strip markdown stars and replace emoji so delivery is more reliable."""
    replacements = {
        "🚨": "[ALERT]",
        "⚠️": "[WARN]",
        "🔎": "[CHECK]",
        "✅": "[OK]",
        "—": "-",
        "–": "-",
        "·": "|",
        "…": "...",
        "*": "",
    }
    text = payload_text
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _log_whatsapp_accept(recipient: str, response_data, *, kind: str) -> None:
    message_id = ""
    status = ""
    if isinstance(response_data, dict):
        messages = response_data.get("messages") or []
        if messages:
            message_id = str(messages[0].get("id") or "")
            status = str(messages[0].get("message_status") or "")
    logger.info(
        "WhatsApp Cloud accepted %s message for %s%s%s",
        kind,
        recipient,
        f" (id={message_id})" if message_id else "",
        f" status={status}" if status else "",
    )
    console.print(
        f"[green]WhatsApp Cloud: accepted {kind} for +{recipient}"
        + (f" · {message_id}" if message_id else "")
        + "[/green]"
    )


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _twilio_whatsapp_address(value: str) -> str:
    if value.lower().startswith("whatsapp:"):
        return value
    digits = _digits_only(value)
    return f"whatsapp:+{digits}" if digits else value


def _send_telegram(token: str, chat_id: str, payload_text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _post_with_retries(
        lambda: requests.post(url, json={"chat_id": chat_id, "text": payload_text}, timeout=30)
    )


def _post_with_retries(post):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = post()
            if not response.ok:
                detail = (response.text or "")[:500]
                raise RuntimeError(f"HTTP {response.status_code}: {detail}")
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}
        except Exception as exc:
            last_error = exc
            logger.warning("Delivery attempt %s failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _missing_line(outcome) -> str:
    if outcome.entity_key == "4430" and outcome.erp is not None:
        who = outcome.entered_by or "an untrusted user"
        return (
            f"4430 — ERP only, {_plain(outcome.erp.qty)} pcs {_item_label(outcome.erp.item)}. "
            f"Entered late {_short_date(outcome.entered_at)} by {who}. {outcome.erp.pointer}"
        )
    if outcome.entity_key == "4421" and outcome.reg and outcome.excel:
        return (
            f"4421 — {_plain(outcome.reg.qty)} {_unit(outcome.reg.uom, outcome.reg.qty)} "
            f"{_item_label(outcome.reg.item)} on the register and Excel, not in ERP. "
            f"{outcome.reg.pointer} {outcome.excel.pointer}"
        )
    if outcome.excel is not None and outcome.entity_key.startswith("RETURN-"):
        return (
            f"Return — Excel only, {_plain(outcome.excel.qty)} pcs torn polybags, "
            f"lot {outcome.excel.lot}. {outcome.excel.pointer}"
        )
    present = [fact.pointer for fact in (outcome.reg, outcome.erp, outcome.excel) if fact]
    return f"{outcome.entity_key} — see {', '.join(present) or 'no source'}"


def _qty_line(outcome) -> str:
    if outcome.entity_key == "4425" and outcome.reg and outcome.erp and outcome.excel:
        over = outcome.excel.qty - (outcome.reg.qty * _factor_from_display(outcome))
        return (
            f"4425 — Excel {_plain(outcome.excel.qty)} m is {_plain(abs(over))} m too high. "
            f"Register {_plain(outcome.reg.qty)} yds matches ERP {_plain(outcome.erp.qty)} m. "
            f"{outcome.excel.pointer}"
        )
    if outcome.entity_key == "4417" and outcome.reg and outcome.erp:
        gap = abs(outcome.erp.qty - outcome.reg.qty)
        return (
            f"4417 — ERP {_plain(outcome.erp.qty)} ctns, register and Excel {_plain(outcome.reg.qty)}. "
            f"Short {_plain(gap)}. {outcome.erp.pointer}"
        )
    if outcome.entity_key == "4419" and outcome.reg and outcome.excel:
        gap = abs(outcome.excel.qty - outcome.reg.qty)
        return (
            f"4419 — Excel {_plain(outcome.excel.qty)} drums, register and ERP {_plain(outcome.reg.qty)}. "
            f"Short {_plain(gap)}. {outcome.excel.pointer}"
        )
    field = outcome.fields[0].field if outcome.fields else "qty"
    return f"{outcome.entity_key} — {field} does not match."


def _duplicate_line(outcome) -> str:
    duplicate = outcome.duplicate_excel[0]
    primary = outcome.excel
    return (
        f"{outcome.entity_key} — Entered twice in Excel, "
        f"+{_plain(duplicate.qty)} {_unit(duplicate.uom, duplicate.qty)}. "
        f"{primary.pointer} {duplicate.pointer}"
    )


def _typo_line(outcome) -> str:
    field = outcome.fields[0]
    if field.field == "lot_no":
        return f"{outcome.entity_key} — Lot: ERP {field.erp}, register and Excel {field.reg}."
    if field.field == "date":
        return (
            f"{outcome.entity_key} — Date: ERP {_short_iso(field.erp)}, "
            f"register and Excel {_short_iso(field.reg)}."
        )
    return f"{outcome.entity_key} — {field.field}: register {field.reg}, ERP {field.erp}, Excel {field.excel}."


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


def message_path(message_type: str) -> Path:
    return ROOT / "outbox" / f"latest_{message_type.lower()}.txt"
