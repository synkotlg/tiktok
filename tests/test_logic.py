"""Logique pure : statistiques de follow-back, choix des sources, prudence, configuration. Pas de navigateur."""
import random
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

import tiktok_rotate as tr

TODAY = date(2026, 10, 15)


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "today", lambda: TODAY)
    return tr.State(tmp_path / "state.db")


def add(st, user, days_ago, source="s", fb_after=None, unf_after=None, fixed=None, note=None, hour=10):
    start = TODAY - timedelta(days=days_ago)
    iso = lambda n: None if n is None else (start + timedelta(days=n)).isoformat()  # noqa: E731
    st.db.execute("INSERT INTO users (username, followed_on, followed_at, followed_back_on, unfollowed_on,"
                  " fixed_days, source, note) VALUES (?,?,?,?,?,?,?,?)",
                  (user, start.isoformat(), f"{start.isoformat()}T{hour:02d}:00:00", iso(fb_after), iso(unf_after),
                   fixed, source, note))
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
    for text in ("Friends", "Amis", "Ami(e)s"):
        assert tr.FRIENDS_RE.match(text) and tr.FOLLOWING_RE.match(text), text
    for text in ("Following", "Abonné(e)", "Follow back"):
        assert not tr.FRIENDS_RE.match(text), text
    assert tr.FOLLOW_API_RE.search("https://www.tiktok.com/api/commit/follow/user/?aid=1988&type=1")
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
    add(st, "oldfan", 40, fb_after=1)
    assert st.due(4, 10) == ["old", "oldfan", "fan"]  # ceux qui n'ont rien rendu d'abord
    assert st.due(4, 10, keep_days=30) == ["old", "oldfan"]  # follow-back gardes 30 jours
    assert st.due(4, 10, keep_followbacks=True, keep_days=30) == ["old"]
    assert st.following() == 5 and st.following(keep_followbacks=True) == 3


def test_phantom_follows_left_out_of_stats_and_caps(st):
    add(st, "real", 5, "a", fb_after=1)
    add(st, "ghost", 5, "a", unf_after=3, note=tr.PHANTOM)       # jamais enregistre par TikTok
    add(st, "late", 5, "a", fb_after=1, unf_after=3, note=tr.PHANTOM)  # a quand meme suivi en retour
    assert st.seed_table(2) == {"a": {"total": 2, "n": 2, "fb": 2}}
    assert sorted(st.samples(60)) == [(1, True), (1, True)]
    assert {u for u, _, _ in st.scored(2)} == {"real", "late"}
    st.mark_unfollowed("real")
    add(st, "gone", 5)
    st.mark_unfollowed("gone", "introuvable", action=False)  # rien clique : hors plafonds
    assert st.unfollowed_today() == 1
    assert len(st.actions_since("unfollow", datetime.now() - timedelta(hours=1))) == 1


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


def test_generic_handles():
    for user in ("user1234567890123", "user8x7k6j5h4", "user123456"):
        assert tr.handle_kind(user) == "generique", user
    for user in ("username", "user_1234567", "user2000", "user12345", "julie.music", "myuser1234567"):
        assert tr.handle_kind(user) == "perso", user


def test_handle_filter_skips_generic_handles_only_when_measured_worse(st):
    cfg = dict(tr.DEFAULTS)
    for i in range(30):
        add(st, f"user{1000000 + i}", 10, fb_after=None)
        add(st, f"perso{i}", 10, fb_after=1 if i < 6 else None)
    random.seed(2)
    wanted = tr.handle_filter(st, cfg)
    assert all(wanted("user9999999") for _ in range(50))  # pas encore handle_filter_min mesures
    for i in range(30, 100):
        add(st, f"user{1000000 + i}", 10, fb_after=1 if i < 32 else None)
        add(st, f"perso{i}", 10, fb_after=1 if i < 50 else None)
    wanted = tr.handle_filter(st, cfg)
    kept = sum(wanted("user9999999") for _ in range(1000))
    assert 50 < kept < 250  # surtout l'exploration (10 %)
    assert all(wanted("julie.music") for _ in range(50))
    assert all(tr.handle_filter(st, dict(cfg, handle_filter=False))("user9999999") for _ in range(50))
    for i in range(100):  # meme rendement : plus rien n'est ecarte, ou presque
        add(st, f"user{2000000 + i}", 10, fb_after=1 if i < 50 else None)
    wanted = tr.handle_filter(st, cfg)
    assert sum(wanted("user9999999") for _ in range(1000)) > 900


def test_segments_by_time_of_day(st):
    add(st, "m1", 10, fb_after=1, hour=9)
    add(st, "e1", 10, hour=20)
    add(st, "e2", 10, fb_after=1, hour=21)
    seg = tr.segment_table(st.scored(2), tr.day_part)
    assert seg == {"matin (avant 12 h)": {"n": 1, "fb": 1}, "soir (apres 18 h)": {"n": 2, "fb": 1}}


def test_unfollows_get_a_share_of_the_day(monkeypatch):
    moment = datetime(2026, 10, 15, 12, 0)
    monkeypatch.setattr(tr, "now", lambda: moment)
    cfg = dict(tr.DEFAULTS, _deadline=moment + timedelta(minutes=100))
    assert tr.unfollow_until(cfg, quota=20) == moment + timedelta(minutes=50)
    assert tr.unfollow_until(cfg, quota=0) is None  # rien a suivre : tout le temps pour les desabonnements
    assert tr.unfollow_until(dict(cfg, _deadline=None), quota=20) is None
    assert tr.unfollow_until(dict(cfg, unfollow_time_share=1), quota=20) is None


def test_part_windows():
    cfg = dict(tr.DEFAULTS)
    w = tr.part_windows(cfg, TODAY)
    assert [x[0] for x in w] == [p[0] for p in tr.DAY_PARTS]
    for name, opens, start, end in w:
        assert opens <= start <= opens + timedelta(minutes=cfg["session_jitter_minutes"]) < end
    assert w[0][1].hour == 8 and w[2][3].hour == 23  # plage horaire [8, 23]
    assert tr.part_windows(cfg, TODAY) == w  # stable pour une date
    assert len({tr.part_windows(cfg, TODAY + timedelta(days=i))[0][2].time() for i in range(10)}) > 1
    assert [x[0] for x in tr.part_windows(dict(cfg, active_hours=[10, 16]), TODAY)] == [p[0] for p in tr.DAY_PARTS[:2]]
    single = tr.part_windows(dict(cfg, day_sessions=False), TODAY)
    assert len(single) == 1 and single[0][1].hour == 8 and single[0][3].hour == 23
    assert tr.current_part(w, w[1][1] + timedelta(minutes=5))[0] == tr.DAY_PARTS[1][0]
    assert tr.current_part(w, w[0][1] - timedelta(minutes=5)) is None


def test_attendance():
    seen = {}
    assert tr.attendance(seen, TODAY, "m", ["a", "s"], 14) == {"a": 0.5, "s": 0.5}  # a priori
    for i in range(1, 9):  # 8 jours avec la session du matin ; l'apres-midi 2 fois, le soir toujours
        seen[(TODAY - timedelta(days=i)).isoformat()] = ["m", "s"] + (["a"] if i <= 2 else [])
    seen[TODAY.isoformat()] = ["m", "a"]  # aujourd'hui ne compte pas
    seen[(TODAY - timedelta(days=30)).isoformat()] = ["m", "a"]  # hors fenetre
    att = tr.attendance(seen, TODAY, "m", ["a", "s"], 14)
    assert att == {"a": pytest.approx(3 / 10), "s": pytest.approx(9 / 10)}


def test_plan_session_shares_the_day():
    theta = {"m": 0.2, "a": 0.2, "s": 0.2}
    later = [("a", 300, 1.0), ("s", 300, 1.0)]
    minutes, quota, ushare = tr.plan_session("m", 240, later, 180, 0, 80, 0, 0, theta, 240, 0.5)
    assert minutes == pytest.approx(60)  # PC toujours allume : 3 sessions egales
    minutes, _, _ = tr.plan_session("m", 240, [("a", 300, 0.5), ("s", 300, 0.5)], 180, 0, 80, 0, 0, theta, 240, 0.5)
    assert minutes == pytest.approx(90)
    minutes, _, _ = tr.plan_session("m", 240, [("a", 300, 0.0), ("s", 300, 0.0)], 180, 0, 80, 0, 0, theta, 240, 0.5)
    assert minutes == pytest.approx(180)  # le PC n'est jamais allume ensuite : tout maintenant
    minutes, quota, _ = tr.plan_session("s", 120, [], 150, 0, 40, 0, 0, theta, 240, 0.5)
    assert minutes == pytest.approx(120) and quota == 40  # derniere partie : tout ce qui reste
    minutes, _, _ = tr.plan_session("m", 200, later, 120, 60, 80, 10, 0, theta, 240, 0.5)
    assert minutes == pytest.approx(0)  # session deja faite (60 min sur 60)
    minutes, _, _ = tr.plan_session("m", 220, later, 160, 20, 80, 3, 0, theta, 240, 0.5)
    assert minutes == pytest.approx(40)  # session interrompue : il reste 40 min


def test_plan_session_puts_follows_in_the_best_parts():
    later = [("a", 300, 1.0), ("s", 300, 1.0)]
    args = dict(window=240, later=later, R=180, used=0, Q=80, done=0, U=90, spf=240, share_u=0.5)
    best = tr.plan_session("m", theta={"m": 0.3, "a": 0.1, "s": 0.2}, **args)
    worst = tr.plan_session("m", theta={"m": 0.1, "a": 0.3, "s": 0.2}, **args)
    # 180 min dont 90 de desabonnements : 22.5 abonnements tiennent ; la meilleure partie en prend 60 min (15)
    assert best[1] == 15 and best[2] == pytest.approx(0)
    middle = tr.plan_session("m", theta={"m": 0.2, "a": 0.3, "s": 0.1}, **args)
    assert middle[1] == 8 and middle[2] == pytest.approx(0.5)  # le reste (7.5) ; desabonnements d'abord
    assert worst[1] == 0 and worst[2] == 1.0  # les deux autres parties suffisent : desabonnements seulement
    few = tr.plan_session("m", theta={"m": 0.3, "a": 0.1, "s": 0.2}, **dict(args, Q=10))
    assert few[1] == 10 and few[2] == pytest.approx(1 / 3)


def test_part_draw_is_stable_within_a_day(st):
    names = [p[0] for p in tr.DAY_PARTS]
    first = tr.part_draw(st, dict(tr.DEFAULTS), names)
    assert set(first) == set(names) and tr.part_draw(st, dict(tr.DEFAULTS), names) == first


def test_action_seconds_default_then_learned(st):
    cfg = dict(tr.DEFAULTS)
    assert tr.action_seconds(st, cfg, "follow") == pytest.approx(180 * 1.05 + 21 + 20)
    tr.learn_seconds(st, cfg, "follow", 300 * 3, 3)  # trop peu d'actions : ignore
    assert tr.action_seconds(st, cfg, "follow") == pytest.approx(230)
    tr.learn_seconds(st, cfg, "follow", 300 * 10, 10)
    assert tr.action_seconds(st, cfg, "follow") == pytest.approx(0.7 * 230 + 0.3 * 300, abs=0.1)


def test_follow_failing():
    assert not tr.follow_failing([False, True, True, False, True])
    assert tr.follow_failing([False, True, True, True])
    assert tr.follow_failing([True, False, True, False, True, False, True])  # un sur deux
    assert not tr.follow_failing([True] * 2 + [False] * 10 + [True, False, True])  # anciens sortis de la fenetre


def test_chunk_size_smaller_when_cautious(st):
    cfg = dict(tr.DEFAULTS)
    assert tr.chunk_size(st, cfg, 50) == 10 and tr.chunk_size(st, cfg, 4) == 4
    st.set_meta("quota_factor", "0.50")  # apres une pause
    assert tr.chunk_size(st, cfg, 50) == 3
    st.set_meta("quota_factor", "1.00")
    assert tr.chunk_size(st, dict(cfg, _slow=1.5), 50) == 3  # captcha resolu dans le passage
    assert tr.chunk_size(st, dict(cfg, verify_every=0), 50) == 10


def test_late_followbacks_do_not_move_the_delay(st):
    cfg = dict(tr.DEFAULTS)
    for i in range(100):  # 10 % suivent en retour le 1er jour
        add(st, f"u{i}", 20, fb_after=1 if i < 10 else None, unf_after=None if i < 10 else 7)
    base = tr.followback_stats(st.samples(60), cfg, 4)["delay"]
    for i in range(100, 108):  # follow-back manques par la lecture, vus au desabonnement (jour 4)
        add(st, f"u{i}", 20, fb_after=4)
        st.db.execute("UPDATE users SET fb_late=1 WHERE username=?", (f"u{i}",))
    st.db.commit()
    assert tr.followback_stats(st.samples(60), cfg, 4)["delay"] == base


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE users (username TEXT PRIMARY KEY, followed_on TEXT NOT NULL, followed_at TEXT NOT NULL,"
               " unfollowed_on TEXT, note TEXT, followed_back_on TEXT, fixed_days INTEGER, unfollowed_at TEXT,"
               " source TEXT)")
    db.execute("INSERT INTO users (username, followed_on, followed_at) VALUES ('x', '2026-10-01', '2026-10-01T10:00:00')")
    db.commit()
    db.close()
    st = tr.State(path)
    st.mark_followed_back("x", late=True)
    assert st.db.execute("SELECT fb_late FROM users").fetchone() == (1,)


def test_reactions_ignore_profile_texts():
    assert tr.PROFILE_TEXT_SEL and "user-bio" in tr.PROFILE_TEXT_SEL


class FakePage:
    def wait_for_timeout(self, ms):
        pass


def test_broken_unfollows_are_never_abandoned(st, monkeypatch):
    """Interface cassee (tous les desabonnements en echec) : 'broken' chaque jour, aucun compte abandonne ; une
    fois reparee, tout est desabonne."""
    day = [TODAY]
    monkeypatch.setattr(tr, "today", lambda: day[0])
    monkeypatch.setattr(tr, "human_pause", lambda *a: None)
    monkeypatch.setattr(tr, "unfollow_one", lambda *a, **k: "failed")
    cfg = dict(tr.DEFAULTS, max_unfollows_per_hour=10**6, active_hours=[0, 24])
    for i in range(5):
        add(st, f"u{i}", 10)
    for _ in range(8):
        cfg["_tried"] = set()
        assert tr.unfollow_pass(FakePage(), st, cfg, [0], 4)[1] == "broken"
        day[0] += timedelta(days=1)
    assert st.count("unfollowed_on IS NULL") == 5
    monkeypatch.setattr(tr, "unfollow_one", lambda *a, **k: "ok")
    cfg["_tried"] = set()
    assert tr.unfollow_pass(FakePage(), st, cfg, [0], 4) == (5, "ok")


def test_unfollow_refused_by_one_account_only(st, monkeypatch):
    """Un compte que TikTok refuse alors que les autres passent : pas de serie, abandonne apres 3 jours."""
    day = [TODAY]
    monkeypatch.setattr(tr, "today", lambda: day[0])
    monkeypatch.setattr(tr, "human_pause", lambda *a: None)
    monkeypatch.setattr(tr, "unfollow_one", lambda page, user, *a, **k: "refused" if user == "bad" else "ok")
    cfg = dict(tr.DEFAULTS, max_unfollows_per_hour=10**6, active_hours=[0, 24])
    for d in range(3):
        add(st, "bad" if d == 0 else f"ok{d}", 10)
        add(st, f"x{d}", 10)
        cfg["_tried"] = set()
        assert tr.unfollow_pass(FakePage(), st, cfg, [0], 4)[1] == "ok"
        day[0] += timedelta(days=1)
    assert st.db.execute("SELECT note FROM users WHERE username='bad'").fetchone() == ("refus",)


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
    day = [TODAY + timedelta(days=1)]
    monkeypatch.setattr(tr, "today", lambda: day[0])
    tr.settle_clean_day(st)
    tr.register_clean_day(st)  # passage propre : le jour ne compte qu'une fois fini
    tr.settle_clean_day(st)
    assert st.get_meta("quota_factor") == "0.25"
    day[0] += timedelta(days=1)
    tr.settle_clean_day(st)
    assert st.get_meta("quota_factor") == "0.35"
    tr.register_clean_day(st)  # session du matin propre...
    tr.register_block(st, cfg, "captcha")  # ...blocage le soir : pas de credit pour ce jour
    day[0] += timedelta(days=1)
    tr.settle_clean_day(st)
    assert st.get_meta("quota_factor") == "0.26"


def test_unfollow_trouble_counts_once_per_day(st, monkeypatch):
    day = [TODAY]
    monkeypatch.setattr(tr, "today", lambda: day[0])
    assert tr.bump_trouble(st, "x") == (1, True)
    assert tr.bump_trouble(st, "x") == (1, False)  # meme jour : pas de nouvel ennui
    day[0] += timedelta(days=1)
    assert tr.bump_trouble(st, "x") == (2, False)
    assert tr.unfollow_trouble(st, "x") == (2, day[0].isoformat())


def test_follow_budget_ramps_up(st):
    cfg = dict(tr.DEFAULTS)
    assert tr.follow_budget(st, cfg) == cfg["ramp_start"]
    for i in range(3):
        add(st, f"u{i}", i + 1)
    assert tr.follow_budget(st, cfg) == cfg["ramp_start"] + 3 * cfg["ramp_step"]
    add(st, "old", cfg["ramp_window_days"] + 5)  # hors fenetre : ne compte pas
    assert tr.follow_budget(st, cfg) == cfg["ramp_start"] + 3 * cfg["ramp_step"]


def test_follow_budget_ramps_again_after_a_break(st):
    cfg = dict(tr.DEFAULTS)
    for i in range(30):
        add(st, f"u{i}", 20 + i)  # actif pendant un mois, puis 20 jours d'absence
    assert tr.follow_budget(st, cfg) == cfg["ramp_start"]


def test_pauses_slow_down_after_a_captcha(monkeypatch):
    waits = []
    page = type("Page", (), {"wait_for_timeout": lambda self, ms: waits.append(ms)})()
    monkeypatch.setattr(tr, "rnd", lambda lo, hi: 1.0)
    cfg = dict(tr.DEFAULTS)
    tr.human_pause(page, cfg, 1, "follow")
    tr.human_pause(page, dict(cfg, _slow=1.5), 1, "follow")
    assert waits == [180000, 270000]


def test_api_reaction():
    assert tr.api_reaction(None) is None and tr.api_reaction((0, "")) is None
    assert tr.api_reaction((10221, "")) == "refused"
    assert tr.api_reaction((10000, "Too many attempts. Try again later.")) == "limit"
    assert tr.api_reaction((2, "Unusual activity detected")) == "warning"


def test_load_config_reads_seeds_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "ROOT", tmp_path)
    (tmp_path / "seeds.txt").write_text("# commentaire\n@Alpha  # note\nbeta\nalpha\n\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"username": "@Moi"}', encoding="utf-8")
    cfg = tr.load_config()
    assert cfg["seeds"] == ["alpha", "beta"] and cfg["seeds_source"] == "seeds.txt"
    assert cfg["username"] == "moi"
