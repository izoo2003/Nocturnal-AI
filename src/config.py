"""Scenario clock, externalized rules, and runtime directories."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
KARACHI = ZoneInfo("Asia/Karachi")
DEFAULT_SCENARIO_NOW = "2026-10-15T08:30:00+05:00"
RULES_PATH = ROOT / "rules_config.json"


class UnitConversions(BaseModel):
    yds_to_m: float = 0.9144


class Tolerances(BaseModel):
    date_days: int = 0
    qty_pct: float = 0.0


class AuditRules(BaseModel):
    trusted_erp_users: list[str] = Field(default_factory=lambda: ["store.clerk"])
    max_entry_delay_hours: int = 24


class Behavior(BaseModel):
    auto_net_returns_by_lot: bool = False


class RulesConfig(BaseModel):
    unit_conversions: UnitConversions = Field(default_factory=UnitConversions)
    tolerances: Tolerances = Field(default_factory=Tolerances)
    audit_rules: AuditRules = Field(default_factory=AuditRules)
    behavior: Behavior = Field(default_factory=Behavior)


def load_environment() -> None:
    load_dotenv(ROOT / ".env")


def get_scenario_now() -> datetime:
    """Return the configured scenario instant in Asia/Karachi."""
    load_environment()
    raw = os.getenv("SCENARIO_NOW", DEFAULT_SCENARIO_NOW).strip()
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KARACHI)
    return parsed.astimezone(KARACHI)


def load_rules(path: Path | None = None) -> RulesConfig:
    rules_path = path or RULES_PATH
    return RulesConfig.model_validate_json(rules_path.read_text(encoding="utf-8"))


def ensure_directories() -> None:
    (ROOT / "data" / "crops").mkdir(parents=True, exist_ok=True)
    (ROOT / "outbox").mkdir(parents=True, exist_ok=True)
