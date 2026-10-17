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


DEFAULT_KEY1_BASE = "gemini-2.5-flash"
DEFAULT_KEY1_FALLBACKS = ("gemini-2.5-flash-lite", "gemini-2.0-flash")
DEFAULT_KEY2_BASE = "gemini-2.5-flash"
DEFAULT_KEY2_FALLBACKS = ("gemini-2.0-flash", "gemini-2.0-flash-lite")


def load_environment() -> None:
    load_dotenv(ROOT / ".env")


def get_gemini_endpoints() -> list[tuple[str, str, str]]:
    """Return (slot, api_key, model) pairs: key1, key2, then one wrap back to key1."""
    load_environment()
    key1 = os.getenv("GEMINI_API_KEY_1", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    key2 = os.getenv("GEMINI_API_KEY_2", "").strip()
    models1 = _slot_models(
        "GEMINI_KEY1_BASE_MODEL",
        "GEMINI_KEY1_FALLBACK_MODELS",
        DEFAULT_KEY1_BASE,
        DEFAULT_KEY1_FALLBACKS,
    )
    models2 = _slot_models(
        "GEMINI_KEY2_BASE_MODEL",
        "GEMINI_KEY2_FALLBACK_MODELS",
        DEFAULT_KEY2_BASE,
        DEFAULT_KEY2_FALLBACKS,
    )
    endpoints: list[tuple[str, str, str]] = []
    if key1:
        endpoints.extend(("key1", key1, model) for model in models1)
    if key2:
        endpoints.extend(("key2", key2, model) for model in models2)
    if key1 and key2:
        endpoints.extend(("key1", key1, model) for model in models1)
    return endpoints


def _slot_models(
    base_env: str,
    fallback_env: str,
    default_base: str,
    default_fallbacks: tuple[str, ...],
) -> list[str]:
    base = os.getenv(base_env, "").strip() or default_base
    models = [base]
    for model in _split_models(os.getenv(fallback_env, ""), default_fallbacks):
        if model not in models:
            models.append(model)
    return models


def _split_models(raw: str, defaults: tuple[str, ...]) -> list[str]:
    if not raw.strip():
        return list(defaults)
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen or list(defaults)


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
