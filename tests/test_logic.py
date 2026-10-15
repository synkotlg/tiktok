"""Logique pure : statistiques de follow-back, choix des sources, prudence, configuration. Pas de navigateur."""
import random
from collections import Counter
from datetime import date, timedelta

import pytest

import tiktok_rotate as tr

TODAY = date(2026, 10, 15)


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "today", lambda: TODAY)
    return tr.State(tmp_path / "state.db")


def add(st, user, days_ago, source="s", fb_after=None, unf_after=None, fixed=None):
    start = TODAY - timedelta(days=days_ago)
    iso = lambda n: None if n is None else (start + timedelta(days=n)).isoformat()  # noqa: E731
    st.db.execute("INSERT INTO users (username, followed_on, followed_at, followed_back_on, unfollowed_on,"
                  " fixed_days, source) VALUES (?,?,?,?,?,?,?)",
                  (user, start.isoformat(), start.isoformat() + "T10:00:00", iso(fb_after), iso(unf_after), fixed, source))
    st.db.commit()


def test_regexes():
    assert tr.FOLLOW_RE.match("Follow") and tr.FOLLOW_RE.match("Suivre")
    assert not tr.FOLLOW_RE.match("Follow back") and not tr.FOLLOW_RE.match("Suivre en retour")
    assert tr.FOLLOW_BACK_RE.match("Suivre en retour") and tr.NOT_FOLLOWING_RE.match("Follow back")
    for text in ("Following", "Abonné(e)", "Abonné", "Suivi(e)", "Friends", "Amis", "Ami(e)s", "Requested",
                 "Demande envoyée"):
        assert tr.FOLLOWING_RE.match(text), text
    for text in ("Follow", "Suivre", "Followers", "Message"):
        assert not tr.FOLLOWING_RE.match(text), text
    assert tr.FOLLOWERS_TAB_RE.match("Followers 1,2 k") and not tr.FOLLOWERS_TAB_RE.match("Following 10")
    assert not tr.FOLLOWERS_TAB_RE.match("Abonnements 10")


def test_km_counts_censored_accounts_as_at_risk_only():
    samples = [(1, True)] * 10 + [(2, False)] * 10 + [(5, False)] * 10
    curve = tr.km_cdf(samples, 5)
    assert curve[1] == (pytest.approx(10 / 30), 30)
    assert curve[3][0] == pytest.approx(10 / 30) and curve[3][1] == 10


def test_followback_stats_adapts_delay():
    cfg = dict(tr.DEFAULTS)
    samples = [(1, True)] * 20 + [(2, True)] * 10 + [(7, False)] * 70
    stats = tr.followback_stats(samples, cfg, base=4)
    assert stats["adapted"] and stats["delay"] == 2
    assert stats["rate"] == pytest.approx(0.30)
    assert tr.followback_stats(samples[:5], cfg, base=4)["delay"] == 4  # pas assez de donnees


def test_due_and_keep_followbacks(st):
    add(st, "old", 5)
    add(st, "fan", 5, fb_after=1)
    add(st, "young", 1)
    add(st, "control", 5, fixed=7)
    assert set(st.due(4, 10)) == {"old", "fan"}
    assert st.due(4, 10, keep_followbacks=True) == ["old"]
    assert st.following() == 4 and st.following(keep_followbacks=True) == 3


def test_seed_table_uses_same_window_for_every_source(st):
    add(st, "a1", 10, "a", fb_after=1)
    add(st, "a2", 10, "a", fb_after=5)  # follow-back trop tardif pour la fenetre de 2 jours
    add(st, "a3", 1, "a", fb_after=0)   # trop recent pour etre compte
    add(st, "b1", 10, "b")
    assert st.seed_table(2) == {"a": {"total": 3, "n": 2, "fb": 1}, "b": {"total": 1, "n": 1, "fb": 0}}


def test_seed_order_prefers_best_source_but_explores(st):
    for i in range(100):
        add(st, f"g{i}", 10, "good", fb_after=1 if i < 30 else None)
        add(st, f"p{i}", 10, "poor", fb_after=1 if i < 2 else None)
    cfg = dict(tr.DEFAULTS)
    random.seed(1)
    firsts = Counter(tr.seed_order(st, cfg, ["poor", "good", "new"])[0] for _ in range(400))
    assert firsts["good"] > firsts["new"] > firsts["poor"]
    assert firsts["new"] > 0  # une source jamais testee garde sa chance


def test_seed_order_puts_dry_sources_last(st):
    cfg = dict(tr.DEFAULTS, seed_scoring=False)
    st.set_meta("seed_dry_until:a", (TODAY + timedelta(days=2)).isoformat())
    st.set_meta("seed_dry_until:c", (TODAY - timedelta(days=1)).isoformat())  # expiree
    assert tr.seed_order(st, cfg, ["a", "b", "c"]) == ["b", "c", "a"]


def test_rest_days_never_consecutive():
    cfg = dict(tr.DEFAULTS, rest_day_chance=0.5)
    days = [TODAY + timedelta(days=i) for i in range(730)]
    rest = [tr.is_rest_day(cfg, d) for d in days]
    assert any(rest) and not any(a and b for a, b in zip(rest, rest[1:]))


def test_block_policy_and_recovery(st, monkeypatch):
    cfg = dict(tr.DEFAULTS)
    assert tr.register_block(st, cfg, "captcha") == (12, 0.75)
    assert tr.register_block(st, cfg, "limit") == (96, 0.375)  # deuxieme blocage : pause doublee
    assert tr.register_block(st, cfg, "warning")[1] == 0.25
    tr.register_clean_day(st)  # meme jour que le blocage : pas de remontee
    assert st.get_meta("quota_factor") == "0.25"
    monkeypatch.setattr(tr, "today", lambda: TODAY + timedelta(days=1))
    tr.register_clean_day(st)
    assert st.get_meta("quota_factor") == "0.35"


def test_follow_budget_ramps_up(st):
    cfg = dict(tr.DEFAULTS)
    assert tr.follow_budget(st, cfg) == cfg["ramp_start"]
    for i in range(3):
        add(st, f"u{i}", i + 1)
    assert tr.follow_budget(st, cfg) == cfg["ramp_start"] + 3 * cfg["ramp_step"]


def test_load_config_reads_seeds_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "ROOT", tmp_path)
    (tmp_path / "seeds.txt").write_text("# commentaire\n@Alpha  # note\nbeta\nalpha\n\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"username": "@Moi"}', encoding="utf-8")
    cfg = tr.load_config()
    assert cfg["seeds"] == ["alpha", "beta"] and cfg["seeds_source"] == "seeds.txt"
    assert cfg["username"] == "moi"
