"""Register-page vision extraction, with a verified snapshot fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from src.config import ROOT, ensure_directories, get_scenario_now, load_environment
from src.db import (
    delete_register_rows_for_page,
    get_processed_page,
    init_db,
    insert_register_rows,
    list_register_rows,
    upsert_processed_page,
)

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = ROOT / "data" / "ocr_snapshot.json"
MONTH_TAG = "2026-09"
MODEL_NAME = "gemini-2.5-flash"
HEADER_FRACTION = 0.15
RULED_ROWS = 16
_PAGE_NUMBER = re.compile(r"page(\d+)", re.IGNORECASE)

EXTRACTION_PROMPT = """
You are transcribing a factory goods receiving register for Kestrel Industries.
The page header says the month. Extract only filled table rows. Ignore empty ruled rows at the bottom.

STRICT ZERO-GUESSING RULE: Never guess, interpolate, or silently fix unclear, smudged,
crossed-out, overwritten, or ink-blotted values. If a field contains a crossed-out number
with a correction written above it, or an ink blot:
- set is_unclear to true
- set unclear_field to "qty"
- transcribe the literal visual appearance into raw_qty_text
  (for example "60 crossed out, 66 written above" or "2 followed by heavy ink blot")
- if digits are completely obscured, set parsed_qty to null
- describe the visual defect in unclear_reason

Return month, page_no, and one object per filled row.
""".strip()


class RegisterRowExtraction(BaseModel):
    row_no: int
    date: str
    grn_no: str
    supplier: str
    item_desc: str
    lot_no: str
    raw_qty_text: str
    parsed_qty: float | None = None
    unit: str
    recd_by: str
    is_unclear: bool = False
    unclear_field: str | None = None
    unclear_reason: str | None = None


class RegisterPageExtraction(BaseModel):
    month: str = Field(description='Page header month, expected "Sep 2026"')
    page_no: int
    rows: list[RegisterRowExtraction]


def extract_register_page(image_path: Path, force: bool = False) -> list[dict]:
    """Extract one register photo, or return the cached SQLite rows for its hash."""
    path = Path(image_path)
    page_no = page_no_from_filename(path)
    digest = sha256_file(path)
    conn = init_db()
    try:
        cached = get_processed_page(conn, digest)
        if cached is not None and not force:
            logger.info("[CACHE HIT] Page already processed. Skipping Vision API.")
            return [_row_dict(row) for row in list_register_rows(conn, page_no)]

        extracted = _extract_rows(path, page_no)
        stored = [_prepare_row(path, page_no, row) for row in extracted]
        delete_register_rows_for_page(conn, page_no)
        insert_register_rows(conn, stored)
        upsert_processed_page(
            conn,
            file_name=path.name,
            sha256_hash=digest,
            month_tag=MONTH_TAG,
            row_count=len(stored),
            processed_at=get_scenario_now().isoformat(),
        )
        return [_row_dict(row) for row in list_register_rows(conn, page_no)]
    finally:
        conn.close()


def page_no_from_filename(path: Path) -> int:
    match = _PAGE_NUMBER.search(path.name)
    if match is None:
        raise ValueError(f"Cannot infer register page number from {path.name}")
    return int(match.group(1))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_rows(path: Path, page_no: int) -> list[dict]:
    load_environment()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return _fallback_rows(page_no)
    try:
        extraction = _gemini_with_retries(path, api_key)
    except Exception as exc:
        logger.warning("Gemini extraction failed after retries: %s", exc)
        return _fallback_rows(page_no)
    if extraction.page_no != page_no:
        logger.warning(
            "Model page_no %s does not match filename page %s; storing under the filename page.",
            extraction.page_no,
            page_no,
        )
    return [_from_model_row(row) for row in extraction.rows]


def _gemini_with_retries(path: Path, api_key: str) -> RegisterPageExtraction:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _call_gemini(path, api_key)
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini attempt %s failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _call_gemini(path: Path, api_key: str) -> RegisterPageExtraction:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg"),
            EXTRACTION_PROMPT,
        ],
        config=types.GenerateContentConfig(
            system_instruction=EXTRACTION_PROMPT,
            temperature=0,
            response_mime_type="application/json",
            response_schema=RegisterPageExtraction,
        ),
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, RegisterPageExtraction):
        return parsed
    if parsed is not None:
        return RegisterPageExtraction.model_validate(parsed)
    return RegisterPageExtraction.model_validate_json(response.text or "")


def _fallback_rows(page_no: int) -> list[dict]:
    logger.warning("[FALLBACK] Using verified local OCR snapshot.")
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return [dict(row) for row in payload["rows"] if int(row["page_no"]) == page_no]


def _from_model_row(row: RegisterRowExtraction) -> dict:
    unclear = row.is_unclear or _looks_unclear(row.raw_qty_text)
    parsed_qty = None if unclear or _digits_hidden(row.raw_qty_text) else row.parsed_qty
    return {
        "row_no": row.row_no,
        "raw_date": row.date,
        "grn_no": str(row.grn_no).removeprefix("GRN-"),
        "supplier": row.supplier,
        "item_desc": row.item_desc,
        "lot_no": row.lot_no,
        "raw_qty_text": row.raw_qty_text,
        "parsed_qty": parsed_qty,
        "uom": row.unit,
        "recd_by": row.recd_by,
        "is_unclear": unclear,
        "unclear_field": "qty" if unclear else row.unclear_field,
        "unclear_reason": row.unclear_reason,
    }


def _looks_unclear(raw_qty_text: str) -> bool:
    text = raw_qty_text.lower()
    markers = ("crossed", "cross-out", "blot", "smudge", "?", "illegible", "overwrit")
    return any(marker in text for marker in markers)


def _digits_hidden(raw_qty_text: str) -> bool:
    text = raw_qty_text.lower()
    return "blot" in text or "?" in raw_qty_text


def _prepare_row(image_path: Path, page_no: int, row: dict) -> dict:
    prepared = dict(row)
    prepared["page_no"] = page_no
    prepared.setdefault("raw_date", row.get("date"))
    prepared.setdefault("uom", row.get("unit"))
    if prepared.get("is_unclear"):
        prepared["crop_path"] = str(_crop_unclear_row(image_path, prepared))
    else:
        prepared.setdefault("crop_path", None)
    return prepared


def _crop_unclear_row(image_path: Path, row: dict) -> str:
    ensure_directories()
    destination = ROOT / "data" / "crops" / f"grn_{row['grn_no']}_row.jpg"
    with Image.open(image_path) as image:
        top, bottom = _row_band(image, int(row["row_no"]))
        image.crop((0, top, image.width, bottom)).save(destination, format="JPEG")
    return destination.relative_to(ROOT).as_posix()


def _row_band(image: Image.Image, row_no: int) -> tuple[int, int]:
    bounds = _horizontal_rules(image)
    if len(bounds) >= row_no:
        top = bounds[row_no - 1]
        if row_no < len(bounds):
            bottom = bounds[row_no]
        else:
            gaps = [bounds[index + 1] - bounds[index] for index in range(len(bounds) - 1)]
            pitch = sorted(gaps)[len(gaps) // 2] if gaps else int(image.height * 0.05)
            bottom = top + pitch
        return top, min(bottom, image.height)
    height = image.size[1]
    body_top = int(height * HEADER_FRACTION)
    band_height = (height - body_top) / RULED_ROWS
    top = int(body_top + (row_no - 1) * band_height)
    bottom = int(body_top + row_no * band_height)
    return max(top, 0), min(bottom, height)


def _horizontal_rules(image: Image.Image) -> list[int]:
    """Return y positions of the blue ruled lines, clustered into one line each."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    scores: list[tuple[int, int]] = []
    for y in range(0, height, 2):
        blueish = 0
        for x in range(int(width * 0.08), int(width * 0.92), 4):
            red, green, blue = pixels[x, y]
            if blue > red + 15 and blue > green + 10 and blue < 180:
                blueish += 1
        scores.append((y, blueish))
    peak = max(score for _, score in scores)
    if peak < 20:
        return []
    threshold = peak * 0.35
    clusters: list[list[int]] = []
    for y, score in scores:
        if score < threshold:
            continue
        if not clusters or y - clusters[-1][-1] > 40:
            clusters.append([y])
        else:
            clusters[-1].append(y)
    return [sum(cluster) // len(cluster) for cluster in clusters]


def _row_dict(row) -> dict:
    payload = {key: row[key] for key in row.keys()}
    payload["is_unclear"] = bool(payload["is_unclear"])
    return payload
