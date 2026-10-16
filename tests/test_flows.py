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

    # jour 3 : a1 et b3 ont suivi en retour ; tout le monde est desabonne sauf le groupe temoin et eux
    # (follow-back gardes followback_keep_days jours)
    mock.my_followers |= {"a1", "b3"}
    day[0] += timedelta(days=2)
    assert tr.cmd_run(cfg_for(follows_per_day=0), here=True, force=True) == 0
    assert users("followed_back_on IS NOT NULL") == {"a1", "b3"}
    fixed = users("fixed_days IS NOT NULL")
    assert users("unfollowed_on IS NOT NULL") == set(src) - fixed - {"a1", "b3"}
    assert mock.following == fixed | {"a1", "b3"} and "sugg1" not in mock.following

    # jour 13, keep_followbacks : le groupe temoin est desabonne, sauf ceux qui ont suivi en retour
    day[0] += timedelta(days=10)
    assert tr.cmd_run(cfg_for(follows_per_day=0, keep_followbacks=True), here=True, force=True) == 0
    assert mock.following == {"a1", "b3"}

    # source introuvable puis captcha non resolu : pause courte, quotas a 75 %
    mock.captcha.add("seedb")
    assert tr.cmd_run(cfg_for(seeds=["ghost", "seedb"], keep_followbacks=True), here=True, force=True) == 0
    st = tr.State(tr.DB_PATH)
    assert st.get_meta("seed_fail:ghost") == "1"
    assert st.get_meta("blocked_until") and st.get_meta("quota_factor") == "0.75"
    mock.captcha.clear()

    # clics ignores 3 fois de suite : limitation silencieuse, pause et quotas a 50 % ; les clics sont notes
    # (plafonds, pas de nouvel essai) mais hors statistiques
    reset_block(day)
    tr.cmd_run(cfg_for(seeds=["seedc"], unfollow_after_days=99), here=True, force=True)
    assert tr.State(tr.DB_PATH).get_meta("quota_factor") == "0.50"
    assert not any(u.startswith("s") for u in mock.following)
    assert users(f"note = '{tr.PHANTOM}' AND unfollowed_at IS NULL") == {"s0", "s1", "s2"}

    # message "Too many attempts" : arret immediat
    reset_block(day)
    tr.cmd_run(cfg_for(seeds=["seedd"], unfollow_after_days=99, explore_share=0), here=True, force=True)
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

    # limitation silencieuse : le bouton passe a Following sans que rien ne soit enregistre ; le sondage
    # de fin de tranche (verify_every = 3) trouve deux abonnements absents : pause, quotas a 50 %
    reset_block(day)
    mock.followers["seedp"] = [f"p{i}" for i in range(10)]
    tr.cmd_run(cfg_for(seeds=["seedp"], unfollow_after_days=99, verify_every=3, explore_share=0),
               here=True, force=True)
    assert tr.State(tr.DB_PATH).get_meta("quota_factor") == "0.50"
    assert len(users(f"username LIKE 'p%' AND note = '{tr.PHANTOM}' AND unfollowed_at IS NULL")) == 2
    assert len(users("username LIKE 'p%' AND unfollowed_on IS NULL")) == 1

    # abonnements disparus constates au desabonnement : rien a cliquer ni a decompter, et blocage apres
    # phantom_streak comptes de suite (d0 et d1, vraiment suivis, sont desabonnes avant ; p2, suivi avant le
    # blocage precedent, est note sans compter : il a deja valu une pause)
    reset_block(day)
    mock.followers["seede"] = [f"e{i}" for i in range(6)]
    tr.cmd_run(cfg_for(seeds=["seede"], unfollow_after_days=99, explore_share=0), here=True, force=True)
    mock.following -= {f"e{i}" for i in range(6)}
    day[0] += timedelta(days=1)
    tr.cmd_run(cfg_for(follows_per_day=0, unfollow_after_days=0, adaptive_delay=False), here=True, force=True)
    st = tr.State(tr.DB_PATH)
    assert st.get_meta("quota_factor") == "0.50"
    assert users(f"unfollowed_at IS NOT NULL AND unfollowed_on = '{day[0].isoformat()}'") == {"d0", "d1"}
    assert st.unfollowed_today() == 2
    phantoms = users(f"note = '{tr.PHANTOM}' AND unfollowed_at IS NULL AND username NOT LIKE 's%'")
    assert phantoms == {"p0", "p1", "p2"} | {f"e{i}" for i in range(5)}
    assert users("username LIKE 'e%' AND unfollowed_on IS NULL") == {"e5"}
    assert mock.following == {"a1", "b3"}

    # refus dans la reponse de TikTok alors que le bouton affiche Following : 3 de suite = limitation
    reset_block(day)
    mock.followers["seedr"] = [f"r{i}" for i in range(10)]
    tr.cmd_run(cfg_for(seeds=["seedr"], unfollow_after_days=99), here=True, force=True)
    assert tr.State(tr.DB_PATH).get_meta("quota_factor") == "0.50"
    assert users(f"username LIKE 'r%' AND note = '{tr.PHANTOM}'") == {"r0", "r1", "r2"}

    # follow-back vu seulement sur le profil au moment du desabonnement (sans lecture de tes abonnes) :
    # note (date incertaine) et garde ; f1 et f2 sont desabonnes, y compris f1 dont la bio ressemble a des
    # messages de TikTok (ni pause, ni compte introuvable)
    reset_block(day)
    mock.followers["seedf"] = ["f0", "f1", "f2"]
    mock.bios["f1"] = "Life moves too fast. Try again later. Couldn't find this account"
    no_scan = dict(adaptive_delay=False, explore_share=0)
    tr.cmd_run(cfg_for(seeds=["seedf"], unfollow_after_days=99, **no_scan), here=True, force=True)
    mock.my_followers.add("f0")
    day[0] += timedelta(days=1)
    tr.cmd_run(cfg_for(follows_per_day=0, unfollow_after_days=0, **no_scan), here=True, force=True)
    assert users("followed_back_on IS NOT NULL AND unfollowed_on IS NULL") == {"a1", "b3", "f0"}
    assert users("fb_late = 1") == {"f0"} and mock.following == {"a1", "b3", "f0"}
    assert users("unfollowed_at IS NOT NULL AND username IN ('f1', 'f2')") == {"f1", "f2"}
    assert not tr.State(tr.DB_PATH).get_meta("blocked_until")

    # sans periode de grace, les follow-back sont desabonnes comme les autres (bouton "Friends")
    tr.cmd_run(cfg_for(follows_per_day=0, unfollow_after_days=0, followback_keep_days=0, **no_scan),
               here=True, force=True)
    assert mock.following == set() and not users("unfollowed_on IS NULL")

    # desabonnements refuses par TikTok (u*) : 3 comptes differents = limitation. Ensuite ils passent apres
    # les autres et ne comptent plus (pas de nouvelle pause a chaque passage) ; refuses 3 fois : abandonnes
    reset_block(day)
    mock.followers["seedu"] = ["u0", "u1", "u2", "z0"]
    tr.cmd_run(cfg_for(seeds=["seedu"], unfollow_after_days=99, **no_scan), here=True, force=True)
    day[0] += timedelta(days=1)
    tr.cmd_run(cfg_for(follows_per_day=0, unfollow_after_days=0, **no_scan), here=True, force=True)
    assert tr.State(tr.DB_PATH).get_meta("quota_factor") == "0.50" and "z0" in mock.following
    reset_block(day)
    tr.cmd_run(cfg_for(follows_per_day=0, unfollow_after_days=0, **no_scan), here=True, force=True)
    assert not tr.State(tr.DB_PATH).get_meta("blocked_until") and mock.following == {"u0", "u1", "u2"}
    tr.cmd_run(cfg_for(follows_per_day=0, unfollow_after_days=0, **no_scan), here=True, force=True)
    assert users("note = 'refus' AND unfollowed_at IS NULL") == {"u0", "u1", "u2"}
    assert not tr.State(tr.DB_PATH).get_meta("blocked_until")

    capsys.readouterr()
    assert tr.cmd_status(cfg_for(seeds=["seeda", "seedb"], seed_scoring=True)) == 0
    out = capsys.readouterr().out
    assert "Rendement des sources" in out and "@seedb" in out and "(retiree de la liste)" in out
    # s0-s2, p0-p2, e0-e5, r0-r2
    assert "Abonnements ignores par TikTok (30 j)      : 15" in out

    assert tr.cmd_diagnose(cfg_for(username="me", seeds=["seedb"])) == 0
    assert list(tr.SHOTS.glob("*-diag-list-seedb.html"))


def test_sessions(env, monkeypatch):
    """Reveils automatiques : rien avant le debut tire de la session (navigateur ferme), la session du matin
    fait sa part des abonnements, celle du soir (la derniere) fait le reste."""
    mock, day = env
    mock.followers = {"seeda": [f"a{i}" for i in range(60)]}
    moment = [datetime.combine(day[0], datetime.min.time()) + timedelta(hours=8)]
    monkeypatch.setattr(tr, "now", lambda: moment[0])
    launched = []
    real_launch = tr.launch
    monkeypatch.setattr(tr, "launch", lambda *a, **k: launched.append(1) or real_launch(*a, **k))
    cfg = cfg_for(seeds=["seeda"], daily_minutes=180, follows_per_day=30, ramp_start=30, rest_day_chance=0)
    windows = tr.part_windows(cfg, day[0])
    assert [w[0] for w in windows] == [p[0] for p in tr.DAY_PARTS]
    st = tr.State(tr.DB_PATH)
    draw = {"matin (avant 12 h)": 0.3, "apres-midi (12-18 h)": 0.1, "soir (apres 18 h)": 0.2}
    st.set_meta("part_draw", json.dumps({"day": day[0].isoformat(), "draw": draw}))
    st.set_meta("sec_per_follow", "230")  # duree reelle (le plafond horaire des tests est sans limite)

    moment[0] = windows[0][2] - timedelta(seconds=1)  # juste avant le debut tire
    assert tr.cmd_run(cfg, here=True, auto=True) == 0 and not launched
    moment[0] = windows[0][2] + timedelta(minutes=1)
    assert tr.cmd_run(cfg, here=True, auto=True) == 0 and launched
    morning = len(users())
    assert 15 <= morning < 30  # sa part : le temps est partage avec les sessions suivantes

    moment[0] = windows[1][2] + timedelta(minutes=1)  # apres-midi : moins bon tirage, la place reste au soir
    tr.cmd_run(cfg, here=True, auto=True)
    assert len(users()) == morning
    moment[0] = windows[2][2] + timedelta(minutes=1)  # soir, derniere session : tout ce qui reste
    tr.cmd_run(cfg, here=True, auto=True)
    assert len(users()) == 30
    seen = json.loads(tr.State(tr.DB_PATH).get_meta("parts_seen"))
    assert seen[day[0].isoformat()] == [p[0] for p in tr.DAY_PARTS]
