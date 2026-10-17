# Kestrel Industries — September 2026 Receiving Reconciliation

## 1. What It Does & How to Run It

Kestrel Industries keeps a handwritten goods-receiving register, an ERP goods-received file, and a storekeeper Excel workbook that never agree. This Task 4 pipeline photographs the September 2026 register, flags only illegible quantities for a person, reconciles all three sources in deterministic Python, proves every row was counted, and sends a short exceptions-first note the Finance Controller can read on a phone in about 30 seconds.

```text
pip install -r requirements.txt
cp .env.example .env
python main.py --auto-resolve-demo
python main.py --interactive-review
python main.py --demo-failure
streamlit run app.py
```

Live logic lives in `pipeline/`. `src/` re-exports those modules for the CLI and Streamlit app.

**What ships**

- Vision OCR via Gemini (`gemini-2.5-flash` and fallbacks) with two API keys: key 1 models, then key 2, then one wrap back to key 1. Leave keys blank to use `data/ocr_snapshot.json`.
- Human review only for unclear rows (GRN `4419`, `4428`) in the CLI or Streamlit.
- Deterministic three-way reconcile against `t4_erp_grn.csv` and `t4_store_sheet.xlsx`. Rules live in `rules_config.json` (date tolerance, qty tolerance, yard factor `0.9144`, late ERP flagging, optional return netting).
- Completeness gate before the official controller summary (Register 28/28, ERP 28/28, Excel 30/30; qty bridges to `113,984` / `115,704.28` / `109,058`).
- Short WhatsApp-ready summary: Missing, Quantity, Typos, then the 100% tie stamp.
- Delivery order: outbox always → Meta WhatsApp Cloud API when enabled → Twilio WhatsApp if configured → Telegram. Only `TEST_RECIPIENT_WHATSAPP` is messaged. Streamlit Send uses `force_send`; CLI re-runs stay idempotent unless `--force-send`.
- Streamlit review desk: OCR cache, HITL crops, proof tables, live rules editor, demo failure, and **Upload extra register pages** (OCR + compare to the same ERP/Excel without changing the September proof).
- Incremental page hashes in SQLite (`kestrel_reconcile.db`). Skip known register pages unless `--force-ocr` / Force Re-Extract.

Happy path prints `100% tied` and writes `outbox/latest_summary.txt`.

## 2. Assumptions Made

- "Now" is `2026-10-15T08:30:00+05:00` (`Asia/Karachi`) from `SCENARIO_NOW`. The wall clock is never used.
- On GRN `4425`, `1 yard = 0.9144 meters`, so register `1200 yds` and ERP `1097.28 M` match. Store sheet `1200 m` is an unconverted copy.
- Supplier names and units are canonicalized for comparison. Raw text and source pointers stay on every figure (`[Reg P2:R05]`, `[ERP R19]`, `[Excel R23]`).
- GRN `4409` appears twice in Excel. The first row is the primary match; the second is kept as a duplicate so counts and sums still include it.
- Extra uploaded photos use page numbers ≥ 101. They never overwrite register pages 1–2 and do not run the September completeness constants. A failed OCR on an upload errors out instead of loading the September snapshot.

## 3. How It Fails, and How Someone Would Know

- **Unresolved handwriting.** If GRN `4419` or `4428` is still unclear, reconciliation stops and the official summary is not sent.
- **Completeness halt.** Every register, ERP, and Excel row must land in exactly one outcome, and the quantity bridges must tie. A miss raises `CompletenessError`, blocks the summary, and sends `[CRITICAL ALERT] Kestrel Reconciliation Failed Safely`. `python main.py --demo-failure` drops a row on purpose to show that path.
- **Retries, snapshot, idempotency.** Vision and messaging retry three times with exponential backoff. Missing keys or a dropped network load `data/ocr_snapshot.json` for the official pages and log the fallback. Each outbound payload is hashed in SQLite; unattended re-runs log `[IDEMPOTENT SKIP]`. Meta temporary tokens expire in about 24 hours; use a Business Manager system-user token for longer life. Free-form Cloud text only delivers after the recipient messages the Meta test number first (24-hour customer-care window).

## 4. How We Handled the Data (Security, Privacy & Anomalies Caught)

Secrets live only in `.env` (gitignored). The dataset is fictional: phone numbers are invalid and emails use `.example`. Nothing is sent to those addresses.

| Ref | What was caught |
| --- | --- |
| 4419 | Qty `60` struck through, `66` written above. Flagged. Excel still `60` drums. |
| 4428 | Ink blot `2??`. Not guessed. ERP and Excel both `250 bdl`. |
| 4412 | Lot `L2609-012` vs ERP `L2609-021`. |
| 4415 | Date 12 Sep vs ERP 13 Sep. |
| 4417 | `48 ctns` vs ERP `46`. |
| 4425 | `1200 yds` = `1097.28 M`. Excel copied `1200 m`. |
| 4421 | `75 rolls` in the register and Excel. Missing in ERP. |
| 4430 | ERP only. `admin2` entered it `2026-10-02 18:47`. |
| 4409 | Excel duplicate. Register and ERP once. |
| Row 31 | Unnumbered `-5000 pcs` return, lot `L2609-002`. |
| 4416 | Excel text date `9/14/2026` normalized to `2026-09-14`. |

## 5. What I Would Do With Another Week

- A WhatsApp reply loop so the storekeeper can confirm a doubtful crop by replying with the number.
- A debit-note table that links negative returns (lot `L2609-002`) to the original GRN.
- Deskew and per-cell boxes so a crumpled factory photo still lands on the right row.

## 6. AI Tools Used & Rough Time Log

Development used Cursor, Gemini for architecture planning, and `gemini-2.5-flash` for runtime vision OCR (with the checked-in snapshot when the API is unavailable).

| Part | Hours |
| --- | --- |
| Data audit and architecture | 1.5 |
| Ingestion and SQLite state | 1.5 |
| Vision OCR and human review | 1.5 |
| Three-way reconciliation and completeness | 1.5 |
| Streamlit UI, messaging, uploads, and docs | 1.0 |
| **Total** | **7.0** |
