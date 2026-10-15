"""SQLite state for page hashes, register rows, review audit, and outbound messages."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.config import ROOT, get_scenario_now

DEFAULT_DB_PATH = ROOT / "kestrel_reconcile.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_pages (
    file_name TEXT NOT NULL,
    sha256_hash TEXT PRIMARY KEY,
    month_tag TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS register_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_no INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    raw_date TEXT,
    grn_no TEXT,
    supplier TEXT,
    item_desc TEXT,
    lot_no TEXT,
    raw_qty_text TEXT,
    parsed_qty REAL,
    uom TEXT,
    recd_by TEXT,
    is_unclear INTEGER NOT NULL DEFAULT 0,
    unclear_field TEXT,
    unclear_reason TEXT,
    crop_path TEXT,
    resolved_qty REAL,
    resolved_by TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS human_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grn_no TEXT NOT NULL,
    field_name TEXT NOT NULL,
    raw_ocr_value TEXT,
    resolved_value TEXT NOT NULL,
    reviewer_note TEXT,
    resolved_by TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id TEXT PRIMARY KEY,
    run_time TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT,
    completeness_passed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    message_hash TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload_text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_processed_page(
    conn: sqlite3.Connection,
    *,
    file_name: str,
    sha256_hash: str,
    month_tag: str,
    row_count: int,
    processed_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO processed_pages (file_name, sha256_hash, month_tag, row_count, processed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sha256_hash) DO UPDATE SET
            file_name = excluded.file_name,
            month_tag = excluded.month_tag,
            row_count = excluded.row_count,
            processed_at = excluded.processed_at
        """,
        (file_name, sha256_hash, month_tag, row_count, processed_at or _now_iso()),
    )
    conn.commit()


def get_processed_page(conn: sqlite3.Connection, sha256_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM processed_pages WHERE sha256_hash = ?",
        (sha256_hash,),
    ).fetchone()


def list_register_rows(conn: sqlite3.Connection, page_no: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM register_rows
            WHERE page_no = ?
            ORDER BY row_no
            """,
            (page_no,),
        )
    )


def list_all_register_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM register_rows ORDER BY page_no, row_no"))


def save_reconciliation_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    summary_json: str,
    completeness_passed: bool,
    run_time: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_runs (
            run_id, run_time, status, summary_json, completeness_passed
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            run_time = excluded.run_time,
            status = excluded.status,
            summary_json = excluded.summary_json,
            completeness_passed = excluded.completeness_passed
        """,
        (run_id, run_time or _now_iso(), status, summary_json, 1 if completeness_passed else 0),
    )
    conn.commit()


def delete_register_rows_for_page(conn: sqlite3.Connection, page_no: int) -> None:
    conn.execute("DELETE FROM register_rows WHERE page_no = ?", (page_no,))
    conn.commit()


def insert_register_rows(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    payload = [_register_tuple(row) for row in rows]
    conn.executemany(
        """
        INSERT INTO register_rows (
            page_no, row_no, raw_date, grn_no, supplier, item_desc, lot_no,
            raw_qty_text, parsed_qty, uom, recd_by, is_unclear, unclear_field,
            unclear_reason, crop_path, resolved_qty, resolved_by, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def replace_register_rows(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    conn.execute("DELETE FROM register_rows")
    conn.commit()
    return insert_register_rows(conn, rows)


def list_unresolved_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM register_rows
            WHERE is_unclear = 1 AND resolved_at IS NULL
            ORDER BY page_no, row_no
            """
        )
    )


def record_human_resolution(
    conn: sqlite3.Connection,
    *,
    grn_no: str,
    field_name: str,
    raw_ocr_value: str | None,
    resolved_value: str,
    reviewer_note: str | None,
    resolved_by: str,
    resolved_at: str | None = None,
) -> None:
    stamped = resolved_at or _now_iso()
    conn.execute(
        """
        INSERT INTO human_resolutions (
            grn_no, field_name, raw_ocr_value, resolved_value, reviewer_note,
            resolved_by, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (grn_no, field_name, raw_ocr_value, resolved_value, reviewer_note, resolved_by, stamped),
    )
    resolved_qty = _as_float(resolved_value) if field_name == "qty" else None
    conn.execute(
        """
        UPDATE register_rows
        SET resolved_qty = COALESCE(?, resolved_qty),
            resolved_by = ?,
            resolved_at = ?
        WHERE grn_no = ?
          AND is_unclear = 1
          AND resolved_at IS NULL
          AND (unclear_field = ? OR unclear_field IS NULL)
        """,
        (resolved_qty, resolved_by, stamped, grn_no, field_name),
    )
    conn.commit()


def get_outbound_message(conn: sqlite3.Connection, message_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outbound_messages WHERE message_hash = ?",
        (message_hash,),
    ).fetchone()


def record_outbound_message(
    conn: sqlite3.Connection,
    *,
    message_hash: str,
    channel: str,
    recipient: str,
    payload_text: str,
    status: str,
    sent_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO outbound_messages (
            message_hash, channel, recipient, payload_text, sent_at, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_hash) DO UPDATE SET
            channel = excluded.channel,
            recipient = excluded.recipient,
            payload_text = excluded.payload_text,
            sent_at = excluded.sent_at,
            status = excluded.status
        """,
        (message_hash, channel, recipient, payload_text, sent_at or _now_iso(), status),
    )
    conn.commit()


def _now_iso() -> str:
    return get_scenario_now().isoformat()


def _as_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _register_tuple(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row["page_no"]),
        int(row["row_no"]),
        row.get("raw_date"),
        row.get("grn_no"),
        row.get("supplier"),
        row.get("item_desc"),
        row.get("lot_no"),
        row.get("raw_qty_text"),
        row.get("parsed_qty"),
        row.get("uom"),
        row.get("recd_by"),
        1 if row.get("is_unclear") else 0,
        row.get("unclear_field"),
        row.get("unclear_reason"),
        row.get("crop_path"),
        row.get("resolved_qty"),
        row.get("resolved_by"),
        row.get("resolved_at"),
    )
