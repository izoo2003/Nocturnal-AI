# Kestrel Industries — Task 4 Register Reconciliation

Three-way goods-receiving reconciliation for **September 2026**. Photograph the handwritten register, compare it to the ERP CSV and the storekeeper Excel, flag only the unclear handwriting for a human, prove nothing was dropped, then send an exceptions-first summary the Finance Controller can read on a phone in about 30 seconds.

Built for the [Nocturnal AI Automation Engineer Assessment](https://github.com/izoo2003/Nocturnal-AI).

## What it does

1. **Extract** register photos with Gemini `gemini-2.5-flash`, or fall back to `data/ocr_snapshot.json` when there is no API key.
2. **Review** only unclear quantities (GRN `4419` strike-through, GRN `4428` ink blot). Never guess digits.
3. **Reconcile** Register, ERP, and Store Sheet in deterministic Python (no LLM math).
4. **Prove** row partition `28/28 | 28/28 | 30/30` and quantity bridges before any message is sent.
5. **Notify** via WhatsApp (Twilio), Telegram, or a local outbox file — idempotent, test recipient only.

Scenario clock is fixed at **15 Oct 2026, 08:30 PKT** (`SCENARIO_NOW`). Do not use the wall clock.

## Quick start

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py --auto-resolve-demo
```

Leave `GEMINI_API_KEY` blank to use the verified OCR snapshot. The controller summary is printed in the terminal and written to `outbox/latest_summary.txt`.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--auto-resolve-demo` | Confirm GRN `4419=66` and `4428=250` without prompts |
| `--interactive-review` | Type those quantities yourself |
| `--force-ocr` | Re-extract even when the page SHA-256 is cached |
| `--demo-failure` | Drop a row in memory; send a critical alert instead of the summary |
| `--force-send` | Bypass message idempotency when re-testing delivery |
| `--month 2026-09` | Month tag (default) |

Phase 1 ingestion check only:

```powershell
python scripts/verify_phase1.py
```

## Inputs (`kestrel-data/`)

| File | Role |
|------|------|
| `t4_register_page1.jpg`, `t4_register_page2.jpg` | Handwritten register, 28 filled rows |
| `t4_erp_grn.csv` | ERP GRNs (28 rows; `4421` and `4429` absent) |
| `t4_store_sheet.xlsx` | Store sheet `Sep-2026` (30 rows) |

Everything in the pack is fictional. Phone numbers are invalid. Emails use `.example`. **Never message dataset numbers or emails** — only the recipient in your `.env`.

## Pipeline

```text
register photos -> vision + uncertainty flags -> HITL for unclear fields
ERP CSV + store sheet -> canonical normalize
all three -> deterministic reconcile -> completeness assertions -> exceptions-first message
page SHA-256 in SQLite (skip seen pages unless --force-ocr)
```

Live-interview knobs live in [`rules_config.json`](rules_config.json) (change in under two minutes):

- `unit_conversions.yds_to_m` (`0.9144`) — Register `1200 yds` equals ERP `1097.28 M`
- `tolerances.date_days` / `qty_pct`
- `audit_rules.trusted_erp_users` / `max_entry_delay_hours` — flags late `GRN-4430` / `admin2`
- `behavior.auto_net_returns_by_lot` — optional netting of Excel row 31 against GRN `4402`

## Layout

```text
main.py                 CLI entrypoint
app.py                  (optional Streamlit review UI — not required for CLI demo)
rules_config.json       Externalized match rules
requirements.txt
.env.example            Safe template — copy to .env locally
kestrel-data/           Assessment inputs (read-only)
data/ocr_snapshot.json  Offline vision fallback
data/crops/             Generated unclear-row strips
src/
  config.py  db.py  extractor.py  loader.py
  review.py  reconcile.py  completeness.py  notifier.py
```

## Safety and what must not be committed

| Never commit | Why |
|--------------|-----|
| `.env` | API keys, tokens, your test WhatsApp/Telegram target |
| `*.db` | Local SQLite run state |
| `outbox/` | Generated messages and reports |
| `data/crops/*.jpg` | Regenerated on extract |

Copy secrets only into a local `.env` from [`.env.example`](.env.example). Keys and tokens must not appear in code, logs, screenshots, or git. Messaging goes only to `TEST_RECIPIENT_WHATSAPP` or `TELEGRAM_CHAT_ID`.

## Expected happy-path stamp

```text
Matched: 22/28 GRNs | Reg: 28/28 rows (113,984) | ERP: 28/28 rows (115,704.28) | Excel: 30/30 rows (109,058) — 100% tied.
```

Exceptions called out first: missing `GRN-4430` / `GRN-4421` / torn return; quantity and unit on `4425`, `4417`, `4419`, `4409` duplicate; lot/date typos on `4412` and `4415`.
