"""
Tests for insights.py -- the generalized proactive-pattern-detection engine
("coaching-engine Phase B" reframed across every domain, not just lifts).
Each detector is tested in isolation with real seeded rows, same discipline
as _rundown_payload/_vitals_trend_text/_last_lift_text elsewhere in this
suite: no real network, no real Claude calls, throwaway SQLite per test.

Not to be confused with tests/test_insights.py, which tests /summary's own
"advisor-style insights" payload (category_insights/top_transactions) -- the
spending detector here reuses that exact same baseline math via
summary._category_insights_for_period rather than re-implementing it.
"""

from datetime import date, timedelta

import db
import insights
from conftest import CHAT


def _insert_expense(chat_id, day, amount, category="Food"):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, expense_date) VALUES (?, ?, 'SGD', ?, 'x', ?, 0, ?)",
            (chat_id, amount, amount, category, day.isoformat()),
        )


def _insert_vitals(chat_id, day, weight_kg=None, sleep_hours=None):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO vitals (chat_id, weight_kg, sleep_hours, vitals_date) VALUES (?, ?, ?, ?)",
            (chat_id, weight_kg, sleep_hours, day.isoformat()),
        )


# ---------- _detect_spending_insights ----------

def test_spending_insight_fires_on_a_real_spike_over_the_baseline():
    db.get_or_create_user(CHAT)
    today = date.today()
    baseline_day = today - timedelta(days=7 * 3)  # inside the 6-period lookback
    _insert_expense(CHAT, baseline_day, 60, category="Dining")  # -> typical_per_period = 10
    _insert_expense(CHAT, today, 100, category="Dining")  # 10x typical, well over $50 floor

    found = insights._detect_spending_insights(CHAT, today.isoformat())
    assert len(found) == 1
    assert found[0]["domain"] == "expense"
    assert found[0]["kind"] == "category_spike"
    assert found[0]["dedup_key"] == "expense:category_spike:Dining"
    assert "Dining" in found[0]["headline"]


def test_spending_insight_skips_a_spike_under_the_absolute_floor():
    """A tiny category reading '10x normal' off a $1 baseline is noise, not
    a real pattern -- SPENDING_SPIKE_MIN_ABSOLUTE exists specifically to
    filter this out."""
    db.get_or_create_user(CHAT)
    today = date.today()
    baseline_day = today - timedelta(days=7 * 3)
    _insert_expense(CHAT, baseline_day, 6, category="Misc")  # typical_per_period = 1
    _insert_expense(CHAT, today, 10, category="Misc")  # 10x typical, but only $10 total

    assert insights._detect_spending_insights(CHAT, today.isoformat()) == []


def test_spending_insight_skips_a_category_with_no_baseline_history():
    """A brand-new category has no 'typical' to compare against -- never
    guess a ratio from nothing."""
    db.get_or_create_user(CHAT)
    today = date.today()
    _insert_expense(CHAT, today, 500, category="Travel")

    assert insights._detect_spending_insights(CHAT, today.isoformat()) == []


def test_spending_insight_returns_nothing_with_no_spending_this_week():
    db.get_or_create_user(CHAT)
    assert insights._detect_spending_insights(CHAT, date.today().isoformat()) == []


# ---------- _detect_vitals_insights ----------

def test_vitals_insight_fires_on_a_real_weight_trend():
    db.get_or_create_user(CHAT)
    today = date.today()
    _insert_vitals(CHAT, today - timedelta(days=6), weight_kg=78.0)
    _insert_vitals(CHAT, today, weight_kg=76.5)  # -1.5kg over the window

    found = insights._detect_vitals_insights(CHAT, today.isoformat())
    weight_insights = [i for i in found if i["kind"] == "weight_trend"]
    assert len(weight_insights) == 1
    assert weight_insights[0]["dedup_key"] == "vitals:weight_trend"
    assert "down" in weight_insights[0]["headline"]
    assert "1.5kg" in weight_insights[0]["headline"]


def test_vitals_insight_skips_a_small_weight_move():
    db.get_or_create_user(CHAT)
    today = date.today()
    _insert_vitals(CHAT, today - timedelta(days=6), weight_kg=78.0)
    _insert_vitals(CHAT, today, weight_kg=77.7)  # only -0.3kg, under the 1.0kg threshold
    assert [i for i in insights._detect_vitals_insights(CHAT, today.isoformat()) if i["kind"] == "weight_trend"] == []


def test_vitals_insight_fires_on_a_real_sleep_drop_vs_prior_week():
    db.get_or_create_user(CHAT)
    today = date.today()
    prior_start = today - timedelta(days=13)
    for i in range(3):
        _insert_vitals(CHAT, prior_start + timedelta(days=i), sleep_hours=7.0)  # prior week avg 7.0h
    for i in range(3):
        _insert_vitals(CHAT, today - timedelta(days=i), sleep_hours=5.0)  # this week avg 5.0h

    found = insights._detect_vitals_insights(CHAT, today.isoformat())
    sleep_insights = [i for i in found if i["kind"] == "sleep_drop"]
    assert len(sleep_insights) == 1
    assert sleep_insights[0]["dedup_key"] == "vitals:sleep_drop"


def test_vitals_insight_skips_sleep_drop_without_enough_prior_history():
    """A drop against nothing isn't a real comparison -- needs real history
    on both sides of the window."""
    db.get_or_create_user(CHAT)
    today = date.today()
    for i in range(3):
        _insert_vitals(CHAT, today - timedelta(days=i), sleep_hours=5.0)
    assert [i for i in insights._detect_vitals_insights(CHAT, today.isoformat()) if i["kind"] == "sleep_drop"] == []


def test_vitals_insight_returns_nothing_with_only_one_checkin():
    db.get_or_create_user(CHAT)
    today = date.today()
    _insert_vitals(CHAT, today, weight_kg=76.0, sleep_hours=6.0)
    assert insights._detect_vitals_insights(CHAT, today.isoformat()) == []


# ---------- _parse_load_kg / _top_load_kg ----------

def test_parse_load_kg_accepts_plain_kg_strings():
    assert insights._parse_load_kg("80kg") == 80.0
    assert insights._parse_load_kg("80.5 kg") == 80.5


def test_parse_load_kg_rejects_unparseable_loads():
    """A machine setting or bodyweight note is just as valid a logged load
    as a real weight (see db.py's lifts docstring) -- but it can't be
    compared numerically, so it must be skipped, never guessed at."""
    assert insights._parse_load_kg("setting 10") is None
    assert insights._parse_load_kg(None) is None
    assert insights._parse_load_kg("bodyweight") is None


def test_top_load_kg_takes_the_max_of_parseable_sets_only():
    sets = [{"reps": 8, "load": "70kg"}, {"reps": 6, "load": "setting 12"}, {"reps": 5, "load": "80kg"}]
    assert insights._top_load_kg(sets) == 80.0


# ---------- _detect_lift_insights ----------

def test_lift_insight_fires_on_a_new_top_set():
    db.get_or_create_user(CHAT)
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    db.add_lift(CHAT, "bench press", "Visa gym", [{"reps": 5, "load": "70kg"}], lift_date=week_ago)
    db.add_lift(CHAT, "bench press", "Visa gym", [{"reps": 5, "load": "75kg"}], lift_date=today.isoformat())

    found = insights._detect_lift_insights(CHAT, today.isoformat())
    progression = [i for i in found if i["kind"] == "progression"]
    assert len(progression) == 1
    assert progression[0]["dedup_key"] == "lift:progression:bench press:75.0"
    assert "75kg" in progression[0]["headline"]
    assert "70kg" in progression[0]["headline"]


def test_lift_insight_skips_when_the_latest_set_does_not_beat_prior_best():
    db.get_or_create_user(CHAT)
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    db.add_lift(CHAT, "squat", "Visa gym", [{"reps": 5, "load": "100kg"}], lift_date=week_ago)
    db.add_lift(CHAT, "squat", "Visa gym", [{"reps": 5, "load": "90kg"}], lift_date=today.isoformat())

    found = [i for i in insights._detect_lift_insights(CHAT, today.isoformat()) if i["kind"] == "progression"]
    assert found == []


def test_lift_insight_skips_progression_when_loads_are_not_parseable():
    db.get_or_create_user(CHAT)
    today = date.today()
    db.add_lift(CHAT, "leg press", "Visa gym", [{"reps": 10, "load": "setting 10"}],
                lift_date=(today - timedelta(days=7)).isoformat())
    db.add_lift(CHAT, "leg press", "Visa gym", [{"reps": 10, "load": "setting 12"}], lift_date=today.isoformat())

    found = [i for i in insights._detect_lift_insights(CHAT, today.isoformat()) if i["kind"] == "progression"]
    assert found == []


def test_lift_insight_fires_stale_when_gap_far_exceeds_typical_cadence():
    db.get_or_create_user(CHAT)
    today = date.today()
    # Four historical sessions roughly every 4 days -- typical_gap ~= 4.
    for i in range(4):
        db.add_lift(CHAT, "pull-ups", "Visa gym", [{"reps": 10, "load": None}],
                     lift_date=(today - timedelta(days=30 - i * 4)).isoformat())
    # Nothing logged since day 30 -- a ~30 day gap, way over 1.5x the ~4 day cadence.

    found = [i for i in insights._detect_lift_insights(CHAT, today.isoformat()) if i["kind"] == "stale"]
    assert len(found) == 1
    assert found[0]["dedup_key"] == "lift:stale:pull-ups"
    assert "pull-ups" in found[0]["headline"]


def test_lift_insight_skips_stale_without_enough_session_history():
    """Needs LIFT_STALE_MIN_SESSIONS real sessions before a 'typical
    cadence' means anything -- a exercise logged only once or twice never
    fires this, however long ago it was."""
    db.get_or_create_user(CHAT)
    today = date.today()
    db.add_lift(CHAT, "deadlift", "Visa gym", [{"reps": 5, "load": "120kg"}],
                lift_date=(today - timedelta(days=60)).isoformat())

    found = [i for i in insights._detect_lift_insights(CHAT, today.isoformat()) if i["kind"] == "stale"]
    assert found == []


def test_lift_insight_returns_nothing_with_no_lifts_logged():
    db.get_or_create_user(CHAT)
    assert insights._detect_lift_insights(CHAT, date.today().isoformat()) == []


# ---------- collect_insights ----------

def test_collect_insights_aggregates_across_every_detector(monkeypatch):
    monkeypatch.setattr(insights, "_DETECTORS", [
        lambda chat_id, today_str: [{"domain": "expense", "kind": "x", "headline": "a", "data": {}, "dedup_key": "a"}],
        lambda chat_id, today_str: [{"domain": "vitals", "kind": "y", "headline": "b", "data": {}, "dedup_key": "b"}],
    ])
    found = insights.collect_insights(CHAT)
    assert {i["dedup_key"] for i in found} == {"a", "b"}


def test_collect_insights_does_no_filtering_of_its_own(monkeypatch):
    """collect_insights is pure aggregation -- dedup/throttle policy lives
    entirely in surfaceable_insights, never here."""
    db.get_or_create_user(CHAT)
    dupe = {"domain": "expense", "kind": "x", "headline": "a", "data": {}, "dedup_key": "a"}
    db.record_insight_sent(CHAT, "a")  # already "sent" -- must NOT be filtered by collect_insights
    monkeypatch.setattr(insights, "_DETECTORS", [lambda chat_id, today_str: [dupe]])
    assert insights.collect_insights(CHAT) == [dupe]


# ---------- surfaceable_insights ----------

def test_surfaceable_insights_excludes_recently_sent_dedup_keys(monkeypatch):
    db.get_or_create_user(CHAT)
    stale_insight = {"domain": "expense", "kind": "x", "headline": "a", "data": {}, "dedup_key": "a"}
    fresh_insight = {"domain": "vitals", "kind": "y", "headline": "b", "data": {}, "dedup_key": "b"}
    db.record_insight_sent(CHAT, "a")
    monkeypatch.setattr(insights, "_DETECTORS", [lambda chat_id, today_str: [stale_insight, fresh_insight]])

    found = insights.surfaceable_insights(CHAT)
    assert found == [fresh_insight]


def test_surfaceable_insights_caps_at_max_per_briefing(monkeypatch):
    db.get_or_create_user(CHAT)
    many = [
        {"domain": "expense", "kind": "x", "headline": f"insight {i}", "data": {}, "dedup_key": f"k{i}"}
        for i in range(5)
    ]
    monkeypatch.setattr(insights, "_DETECTORS", [lambda chat_id, today_str: many])
    assert len(insights.surfaceable_insights(CHAT)) == insights.MAX_INSIGHTS_PER_BRIEFING
