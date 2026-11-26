"""Runtime configuration, read once from the environment (and `.env` locally).

Every knob has a safe default. Invalid values degrade to the default with a
warning instead of crashing at import time (the v1 `assert` in config.py killed
whole runs when a secret like WEEKLY_TARGET=25 was set).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # optional locally
        return
    load_dotenv(REPO_ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return default if val is None or val.strip() == "" else val.strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    log.warning("Config %s=%r is not a boolean; using default %s", name, raw, default)
    return default


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = _env(name, "")
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("Config %s=%r is not an integer; using default %d", name, raw, default)
        return default
    if (lo is not None and val < lo) or (hi is not None and val > hi):
        log.warning("Config %s=%d outside [%s, %s]; using default %d", name, val, lo, hi, default)
        return default
    return val


class Keys(BaseModel):
    """Provider credentials. Values are never logged; use `configured()` for reporting."""

    anthropic: str = ""
    groq: str = ""
    gemini: str = ""
    serper: str = ""
    tavily: str = ""
    hunter: str = ""
    prospeo: str = ""
    findymail: str = ""
    leadmagic: str = ""
    anymailfinder: str = ""
    apollo: str = ""
    zerobounce: str = ""
    millionverifier: str = ""
    reoon: str = ""
    bouncer: str = ""
    sender_email: str = ""
    gmail_app_password: str = ""

    ENV_NAMES: dict[str, str] = Field(
        default={
            "anthropic": "ANTHROPIC_API_KEY",
            "groq": "GROQ_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "serper": "SERPER_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "hunter": "HUNTER_API_KEY",
            "prospeo": "PROSPEO_API_KEY",
            "findymail": "FINDYMAIL_API_KEY",
            "leadmagic": "LEADMAGIC_API_KEY",
            "anymailfinder": "ANYMAILFINDER_KEY",
            "apollo": "APOLLO_API_KEY",
            "zerobounce": "ZEROBOUNCE_API_KEY",
            "millionverifier": "MILLIONVERIFIER_API_KEY",
            "reoon": "REOON_API_KEY",
            "bouncer": "BOUNCER_API_KEY",
            "sender_email": "SENDER_EMAIL",
            "gmail_app_password": "GMAIL_APP_PASSWORD",
        },
        exclude=True,
    )

    def configured(self) -> dict[str, bool]:
        return {env: bool(getattr(self, field)) for field, env in self.ENV_NAMES.items()}


class Settings(BaseModel):
    outreach_mode: Literal["internship", "job"] = "internship"
    dry_run: bool = True
    force_window: bool = False
    attach_resume: bool = False
    auto_approve: bool = True

    # Volumes
    weekly_source_target: int = 80
    research_per_tick: int = 18
    drafts_per_tick: int = 14
    daily_send_cap: int = 16
    weekly_send_cap: int = 70
    per_tick_send_cap: int = 8
    apollo_match_budget: int = 10
    search_budget_per_tick: int = 60

    # Sending behaviour
    send_weekdays: tuple[int, ...] = (0, 1, 2, 3)  # Mon–Thu, recipient-local
    send_windows: tuple[tuple[int, int], ...] = ((9 * 60 + 30, 11 * 60 + 30), (14 * 60, 16 * 60))
    min_send_delay_s: int = 45
    max_send_delay_s: int = 150
    followup_gaps_days: tuple[int, ...] = (7, 7)
    max_followups: int = 2

    # Qualification
    tier_a_min: int = 80
    tier_b_min: int = 60
    catch_all_min_confidence: int = 70
    reverify_after_days: int = 45

    # Verification
    smtp_probe_enabled: bool = True
    smtp_helo_domain: str = "gmail.com"

    # LLM routing
    llm_provider: str = "auto"  # auto | anthropic | groq | gemini
    llm_fast_model: str = ""
    llm_writer_model: str = ""

    # Paths
    state_dir: Path = REPO_ROOT / "state"
    inputs_dir: Path = REPO_ROOT / "inputs"
    resume_path: Path = REPO_ROOT / "resume.pdf"

    # Wall-clock budget for one tick (GitHub job timeout is higher)
    tick_budget_s: int = 30 * 60

    keys: Keys = Field(default_factory=Keys)

    @property
    def can_send_mail(self) -> bool:
        return bool(self.keys.sender_email and self.keys.gmail_app_password)


def load_settings(**overrides) -> Settings:
    _load_dotenv()
    mode = _env("OUTREACH_MODE", "internship").lower()
    if mode not in ("internship", "job"):
        log.warning("OUTREACH_MODE=%r invalid; using 'internship'", mode)
        mode = "internship"

    keys = Keys(**{field: _env(env) for field, env in Keys().ENV_NAMES.items()})
    data = dict(
        outreach_mode=mode,
        dry_run=_bool("DRY_RUN", True),
        force_window=_bool("FORCE_WINDOW", False),
        attach_resume=_bool("ATTACH_RESUME", False),
        auto_approve=_bool("AUTO_APPROVE", True),
        weekly_source_target=_int("WEEKLY_TARGET", 80, 0, 500),
        research_per_tick=_int("RESEARCH_PER_TICK", 18, 0, 200),
        drafts_per_tick=_int("DRAFTS_PER_TICK", 14, 0, 100),
        daily_send_cap=_int("DAILY_SEND_CAP", 16, 0, 100),
        weekly_send_cap=_int("WEEKLY_CAP", 70, 0, 500),
        per_tick_send_cap=_int("MAX_PER_RUN", 8, 0, 50),
        apollo_match_budget=_int("APOLLO_MATCH_BUDGET", 10, 0, 500),
        search_budget_per_tick=_int("SEARCH_BUDGET_PER_TICK", 60, 0, 1000),
        min_send_delay_s=_int("MIN_SEND_DELAY_S", 45, 0, 3600),
        max_send_delay_s=_int("MAX_SEND_DELAY_S", 150, 0, 3600),
        tier_a_min=_int("TIER_A_MIN", 80, 1, 100),
        tier_b_min=_int("TIER_B_MIN", 60, 1, 100),
        catch_all_min_confidence=_int("CATCH_ALL_MIN_CONFIDENCE", 70, 0, 100),
        smtp_probe_enabled=_bool("SMTP_VERIFY_ENABLED", True),
        llm_provider=_env("LLM_PROVIDER", "auto").lower(),
        llm_fast_model=_env("LLM_FAST_MODEL"),
        llm_writer_model=_env("LLM_WRITER_MODEL"),
        tick_budget_s=_int("TICK_BUDGET_S", 30 * 60, 60, 6 * 3600),
        keys=keys,
    )
    if state := _env("STATE_DIR"):
        data["state_dir"] = Path(state).resolve()
    if inputs := _env("INPUTS_DIR"):
        data["inputs_dir"] = Path(inputs).resolve()
    data.update(overrides)
    s = Settings(**data)
    if s.max_send_delay_s < s.min_send_delay_s:
        s.max_send_delay_s = s.min_send_delay_s
    if s.tier_b_min >= s.tier_a_min:
        log.warning("TIER_B_MIN >= TIER_A_MIN; resetting to 60/80")
        s.tier_a_min, s.tier_b_min = 80, 60
    return s
