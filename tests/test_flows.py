"""Parcours complets (run, status, diagnose) contre la maquette locale de TikTok, sans reseau.

Necessite un Chromium pour Playwright : TIKTOK_TEST_CHROMIUM (chemin de l'executable),
/opt/pw-browsers/chromium, ou celui de 'playwright install chromium'. Sinon le test est ignore.
"""
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import tiktok_rotate as tr
from mock_tiktok import MockTikTok


def chromium_path():
    for path in (os.environ.get("TIKTOK_TEST_CHROMIUM"), "/opt/pw-browsers/chromium"):
        if path and Path(path).exists():
            return path
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    exe = chromium_path()
    mock = MockTikTok()
    day = [date(2026, 10, 15)]
    for name, value in (("ROOT", tmp_path), ("DATA", tmp_path / "data"), ("PROFILE", tmp_path / "data" / "profile"),
                        ("DB_PATH", tmp_path / "data" / "state.db"), ("SHOTS", tmp_path / "data" / "debug")):
        monkeypatch.setattr(tr, name, value)
    tr.PROFILE.mkdir(parents=True)
    monkeypatch.setattr(tr, "rnd", lambda lo, hi: lo / 4)
    monkeypatch.setattr(tr, "human_pause", lambda *a: None)
    monkeypatch.setattr(tr, "today", lambda: day[0])
    monkeypatch.setattr(tr, "now", lambda: datetime.combine(day[0], datetime.now().time().replace(hour=12)))

    def launch(p, cfg, hidden):
        try:
            ctx = p.chromium.launch_persistent_context(str(tr.PROFILE), headless=True, executable_path=exe)
        except Exception as e:
            pytest.skip(f"Chromium indisponible : {e}")
        ctx.route("https://www.tiktok.com/**", mock.handler)
        return ctx
    monkeypatch.setattr(tr, "launch", launch)
    return mock, day


def cfg_for(**kw):
    cfg = tr.load_config()
    cfg.update(seeds=["seeda", "seedb"], max_follows_per_hour=3600, max_unfollows_per_hour=3600,
               daily_minutes=0, ramp_start=30, follows_per_day=30, unfollow_after_days=2,
               max_scrolls_per_seed=10, seed_scoring=False, captcha_wait_minutes=0)
    cfg.update(kw)
    return cfg


def users(where="1=1"):
    return {r[0] for r in tr.State(tr.DB_PATH).db.execute(f"SELECT username FROM users WHERE {where}")}


def reset_block(day):
    day[0] += timedelta(days=1)
    st = tr.State(tr.DB_PATH)
    for key, value in (("blocked_until", ""), ("quota_factor", "1.0"), ("block_streak", "0")):
        st.set_meta(key, value)


def test_rotation(env, capsys):
    mock, day = env
    mock.my_followers = {"oldfan1"}
    mock.followers = {"seeda": [f"a{i}" for i in range(6)] + ["oldfan1"],  # oldfan1 : "Follow back", ignore
                      "seedb": [f"b{i}" for i in range(40)],                # 4 pages, chargees au defilement
                      "seedc": [f"s{i}" for i in range(10)], "seedd": [f"d{i}" for i in range(10)]}

    # jour 1 : seeda epuisee apres 6 comptes, la suite vient de seedb (defilement necessaire)
    assert tr.cmd_run(cfg_for(), here=True, force=True) == 0
    st = tr.State(tr.DB_PATH)
    src = dict(st.db.execute("SELECT username, source FROM users"))
    assert {u for u, s in src.items() if s == "seeda"} == {f"a{i}" for i in range(6)}
    assert len(src) == 30 and mock.following == set(src)
    assert "oldfan1" not in mock.following and "sugg1" not in mock.following
    assert st.get_meta("seed_dry_until:seeda") and not st.get_meta("seed_dry_until:seedb")
    assert json.loads((tr.ROOT / "config.json").read_text())["username"] == "me"

    # jour 3 : a1 et b3 ont suivi en retour ; tout le monde est desabonne sauf le groupe temoin
    mock.my_followers |= {"a1", "b3"}
    day[0] += timedelta(days=2)
    assert tr.cmd_run(cfg_for(follows_per_day=0), here=True, force=True) == 0
    assert users("followed_back_on IS NOT NULL") == {"a1", "b3"}
    fixed = users("fixed_days IS NOT NULL")
    assert users("unfollowed_on IS NOT NULL") == set(src) - fixed
    assert mock.following == fixed and "sugg1" not in mock.following

    # jour 13, keep_followbacks : le groupe temoin est desabonne, sauf ceux qui ont suivi en retour
    day[0] += timedelta(days=10)
    assert tr.cmd_run(cfg_for(follows_per_day=0, keep_followbacks=True), here=True, force=True) == 0
    assert mock.following == fixed & {"a1", "b3"}

    # source introuvable puis captcha non resolu : pause courte, quotas a 75 %
    mock.captcha.add("seedb")
    assert tr.cmd_run(cfg_for(seeds=["ghost", "seedb"], keep_followbacks=True), here=True, force=True) == 0
    st = tr.State(tr.DB_PATH)
    assert st.get_meta("seed_fail:ghost") == "1"
    assert st.get_meta("blocked_until") and st.get_meta("quota_factor") == "0.75"
    mock.captcha.clear()

    # clics ignores 3 fois de suite : limitation silencieuse, pause et quotas a 50 %
    reset_block(day)
    tr.cmd_run(cfg_for(seeds=["seedc"], unfollow_after_days=99), here=True, force=True)
    assert tr.State(tr.DB_PATH).get_meta("quota_factor") == "0.50"
    assert not any(u.startswith("s") for u in mock.following)

    # message "Too many attempts" : arret immediat
    reset_block(day)
    tr.cmd_run(cfg_for(seeds=["seedd"], unfollow_after_days=99), here=True, force=True)
    assert tr.State(tr.DB_PATH).get_meta("quota_factor") == "0.50"
    assert {u for u in mock.following if u.startswith("d")} == {"d0", "d1"}

    # captcha des l'accueil : pause, sans lecture des abonnes ni abonnement
    reset_block(day)
    known = users()
    mock.captcha.add("home")
    tr.cmd_run(cfg_for(), here=True, force=True)
    st = tr.State(tr.DB_PATH)
    assert users() == known and st.get_meta("quota_factor") == "0.75"
    assert st.get_meta("last_scan_on") != day[0].isoformat()
    mock.captcha.clear()

    capsys.readouterr()
    assert tr.cmd_status(cfg_for(seeds=["seeda", "seedb"], seed_scoring=True)) == 0
    out = capsys.readouterr().out
    assert "Rendement des sources" in out and "@seedb" in out and "(retiree de la liste)" in out

    assert tr.cmd_diagnose(cfg_for(username="me", seeds=["seedb"])) == 0
    assert list(tr.SHOTS.glob("*-diag-list-seedb.html"))
