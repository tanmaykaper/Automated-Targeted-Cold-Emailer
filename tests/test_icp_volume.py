from __future__ import annotations

import random

from outreach.icp import canonical_role, company_type, title_tier
from outreach.mailer import daily_budget, run_budget
from outreach.models import Company, Lead, Person
from outreach.profile import DISCOVERY_ROTATION, VERTICALS
from outreach.settings import load_settings


def test_title_alternates_map_to_tiers():
    assert title_tier("Brand Head - Man Matters")[0] == "B"
    assert title_tier("Segment Lead, Snacks")[0] == "B"
    assert title_tier("Head of Brands")[0] == "B"
    assert title_tier("Office of the CEO")[0] == "A"
    assert title_tier("Senior Brand Manager")[0] == "C"
    assert title_tier("Talent Acquisition Lead")[0] is None
    assert canonical_role("Strategy Head") == "Head of Strategy"


def test_startups_rank_below_tier1_above_research():
    from outreach.scoring import _company
    def lead(name, vertical, desc=""):
        return Lead(id="x", vertical=vertical, person=Person(full_name="A B"), company=Company(name=name, description=desc))
    tier1 = _company(lead("McKinsey & Company", "CONSULTING")).points
    startup = _company(lead("Mosaic Wellness", "STARTUP_BRANDS")).points
    research = _company(lead("Centre for Policy", "RESEARCH_ECON", "think tank")).points
    assert tier1 > startup > research
    assert company_type(lead("Mosaic Wellness", "STARTUP_BRANDS")) == "startup"


def test_rotation_includes_non_tech_startups_india_and_abroad():
    entries = [e for slot in DISCOVERY_ROTATION for e in slot]
    assert all(v in VERTICALS for _, v in entries)
    non_tech = {"STARTUP_BRANDS", "STARTUP_FOOD", "STARTUP_PRODUCT"}
    assert sum(v in non_tech for _, v in entries) >= 16
    assert {g for g, v in entries if v in non_tech} & {"London", "Dubai", "Singapore", "New York", "San Francisco"}


def test_daily_budget_random_but_stable_per_day():
    s = load_settings()
    days = [f"2026-12-{d:02d}" for d in range(1, 29)]
    vals = [daily_budget(s, d) for d in days]
    assert all(s.daily_send_floor <= v <= s.daily_send_cap <= 24 for v in vals)
    assert len(set(vals)) > 3
    assert daily_budget(s, days[0]) == daily_budget(s, days[0])


def test_run_budget_varies_and_sometimes_skips():
    s = load_settings()
    rng = random.Random(1)
    vals = [run_budget(s, rng) for _ in range(500)]
    assert 0 in vals and max(vals) == s.per_tick_send_cap and len(set(vals)) >= 4
