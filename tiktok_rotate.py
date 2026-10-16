#!/usr/bin/env python3
"""
tiktok_rotate.py - Rotation d'abonnements TikTok, orientee follow-back.

Chaque jour : lecture de tes abonnes (mesure des follow-back), desabonnement des comptes suivis par ce
script depuis N jours, puis abonnement a de nouveaux comptes pris dans les abonnes de comptes sources.
Les sources dont les abonnes suivent le plus en retour sont privilegiees (echantillonnage de Thompson).
Le navigateur utilise un profil persistant : connexion manuelle une seule fois.

  python tiktok_rotate.py login         connexion manuelle (une seule fois)
  python tiktok_rotate.py run           passe quotidienne (--here : rester sur le bureau courant)
  python tiktok_rotate.py status        etat de la rotation et rendement de chaque source
  python tiktok_rotate.py check-seeds   verifie que les comptes sources existent
  python tiktok_rotate.py diagnose      teste les selecteurs sur ton profil et une source (aucune action)
"""
import argparse
import atexit
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PROFILE = DATA / "profile"
DB_PATH = DATA / "state.db"
LOCK = DATA / "run.lock"
SHOTS = DATA / "debug"

# ---------------------------------------------------------------------------
# UI : tout ce qui depend du texte ou du DOM de TikTok (FR + EN) est ici.
# Pas de \b dans les motifs partages avec le JS (JS_PROFILE_BTN) : en JS, \b ignore les accents.
# ---------------------------------------------------------------------------
TT = "https://www.tiktok.com"
FOLLOW_RE = re.compile(r"^(Suivre|Follow)$", re.I)  # pas encore abonne (et lui non plus)
FOLLOW_BACK_RE = re.compile(r"^(Suivre en retour|Follow back)$", re.I)  # il te suit deja
NOT_FOLLOWING_RE = re.compile(r"^(Suivre|Follow)( en retour| back)?$", re.I)
FOLLOWING_RE = re.compile(
    r"^(Abonné\(e\)|Abonnée?|Suivi\(e\)|Suivie?|Following|Ami\(e\)s|Amis|Friends|"
    r"Demande envoyée|Demandé|Requested)", re.I)
UNFOLLOW_RE = re.compile(r"^(Ne plus suivre|Se désabonner|Unfollow|Annuler la demande|Cancel request)$", re.I)
LATER_RE = re.compile(r"^(Plus tard|Pas maintenant|Not now|Peut-être plus tard|Maybe later)$", re.I)
COOKIES_RE = re.compile(r"^(Refuser les cookies optionnels|Decline optional cookies)$", re.I)
FOLLOWERS_TAB_RE = re.compile(r"^(Followers?|Abonnés|Abonné\(e\)s)", re.I)
# Reactions de TikTok, de la moins grave a la plus grave :
BLOCK_RE = re.compile(  # rythme trop rapide
    r"Trop de tentatives|Too many attempts|Nombre maximal de tentatives|Maximum number of attempts|"
    r"trop rapidement|too fast|Réessayez plus tard|Try again later", re.I)
CAPTCHA_SEL = ('#captcha-verify-container-main-page, #captcha_container, .captcha_verify_container, '
               '.captcha-verify-container, [class*="captcha_verify"], [class*="captcha-verify"], '
               '[id*="captcha-verify"]')
CAPTCHA_RE = re.compile(  # puzzle ou rotation a resoudre : soupcon de bot, a resoudre a la main
    r"Faites glisser le curseur|Drag the slider|Vérifiez pour continuer|Verify to continue|"
    r"Sélectionnez 2 objets|Select 2 objects", re.I)
WARN_RE = re.compile(  # automatisation soupconnee
    r"activité inhabituelle|unusual activity|activité suspecte|suspicious activity|"
    r"comportement automatisé|automated behavior", re.I)
ATTENTION_RE = re.compile(  # compte banni, suspendu, deconnecte
    r"Votre compte a été (banni|suspendu|désactivé)|Your account (was|has been) "
    r"(permanently )?(banned|suspended|disabled)", re.I)
ATTENTION_URL_RE = re.compile(r"tiktok\.com/(login|signup)")
NOT_FOUND_RE = re.compile(
    r"Impossible de trouver ce compte|Couldn.t find this account|Ce compte est introuvable|"
    r"Ce compte a été banni|This account was banned|Page non disponible|Page not available", re.I)
LOGGED_IN_SEL = '[data-e2e="profile-icon"], a[data-e2e="nav-profile"][href*="/@"]'
LOGIN_SEL = '[data-e2e="top-login-button"], #header-login-button'
NAV_PROFILE_SEL = 'a[data-e2e="nav-profile"][href*="/@"], a:has([data-e2e="profile-icon"])'
USER_TITLE_SEL = '[data-e2e="user-title"]'
FOLLOWERS_COUNT_SEL = '[data-e2e="followers-count"]'
FOLLOWERS_LABEL_SEL = '[data-e2e="followers"]'
DIALOG_SEL = 'div[role="dialog"], section[role="dialog"], [data-e2e="follow-info-popup"]'
ROW_LINK_SEL = 'a[href*="/@"]'
PROFILE_BTN_SEL = '[data-rot-btn]'
# Pseudo attribue d'office par TikTok a l'inscription ("user" suivi de chiffres) : souvent un compte qui
# regarde sans publier. Sert seulement a mesurer si ces comptes suivent moins en retour (handle_filter).
GENERIC_HANDLE_RE = re.compile(r"^user(?=(?:[a-z]*\d){5})[a-z0-9]{6,}$")

# Pour chaque bouton de la liste : pseudo de sa ligne. On remonte les ancetres jusqu'au premier qui
# contient un lien de profil ; s'il en contient plusieurs (ligne sans lien, on a remonte jusqu'a la
# liste entiere), on renvoie null plutot que le pseudo d'un autre.
JS_USERS = r"""els => els.map(el => {
  const user = a => {
    const m = (a.getAttribute('href') || '').match(/\/@([\w.]+)\/?(?:[?#].*)?$/);
    return m ? m[1].toLowerCase() : null;
  };
  let n = el;
  for (let i = 0; i < 8 && n; i++) {
    n = n.parentElement;
    if (!n) break;
    const found = new Set([...n.querySelectorAll('a[href*="/@"]')].map(user).filter(Boolean));
    if (found.size === 1) return [...found][0];
    if (found.size > 1) return null;
  }
  return null;
})"""

# Pour chaque lien de la liste : pseudo cible (sert a lire la liste de TES abonnes).
JS_HREF_USERS = r"""els => els.map(a => {
  const m = (a.getAttribute('href') || '').match(/\/@([\w.]+)\/?(?:[?#].*)?$/);
  return m ? m[1].toLowerCase() : null;
})"""

# Bouton Suivre / Abonne de l'en-tete d'un profil : le plus proche du titre (les comptes suggeres plus
# bas ont aussi des boutons Suivre). Il est marque data-rot-btn ; retourne son texte, ou null.
JS_PROFILE_BTN = r"""args => {
  document.querySelectorAll('[data-rot-btn]').forEach(e => e.removeAttribute('data-rot-btn'));
  const re = new RegExp(args.re, 'i');
  const label = b => (b.innerText || b.getAttribute('aria-label') || '').trim();
  let n = document.querySelector(args.title);
  for (let i = 0; i < 10 && n; i++) {
    n = n.parentElement;
    if (!n) break;
    const hit = [...n.querySelectorAll('button, [data-e2e="follow-button"]')].find(b =>
      b.getClientRects().length > 0 && (b.matches('[data-e2e="follow-button"]') || re.test(label(b))));
    if (hit) {
      hit.setAttribute('data-rot-btn', '1');
      return label(hit);
    }
  }
  return null;
}"""

# Element defilant principal d'une liste (le plus haut) : position, ou defilement de dy pixels.
JS_SCROLLER = r"""(root, dy) => {
  const els = [root, ...root.querySelectorAll('*')].filter(e => e.scrollHeight > e.clientHeight + 20
    && /(auto|scroll)/.test(getComputedStyle(e).overflowY));
  if (!els.length) return null;
  const el = els.reduce((a, b) => (b.clientHeight > a.clientHeight ? b : a));
  if (dy) el.scrollBy(0, dy);
  return el.scrollTop;
}"""

# ---------------------------------------------------------------------------
# Configuration (surchargee par config.json)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "username": "",
    # sources : seeds.txt (ou la cle "seeds" de config.json). Par tranches de follows_per_seed abonnements :
    # apres chaque tranche la source est tiree a nouveau, et sa liste rouverte montre ses abonnes les plus
    # recents (en ligne a cet instant). Au plus seeds_per_run sources epuisees ou inaccessibles par passage.
    "seeds_per_run": 6,
    "follows_per_seed": 15,
    "follows_per_day": 80,
    "unfollow_after_days": 4,
    # true : on reste abonne aux comptes qui ont suivi en retour (ils ne sont plus desabonnes)
    "keep_followbacks": False,
    # sinon, ils restent suivis au moins ce nombre de jours (0 : meme delai que les autres) : se desabonner
    # juste apres leur follow-back les pousse a se desabonner a leur tour
    "followback_keep_days": 30,
    # choix des sources selon leur taux de follow-back (false : simple rotation dans l'ordre de seeds.txt)
    "seed_scoring": True,
    "seed_score_days": 2,
    "seed_prior_strength": 20,
    "seed_dry_days": 3,
    # pseudos generiques (user123...) evites si leur follow-back mesure est nettement plus faible
    "handle_filter": True,
    "handle_filter_min": 40,
    "handle_filter_ratio": 0.7,
    "handle_filter_explore": 0.1,
    # prudence : montee en charge, plafonds, plage horaire, repos, reaction aux blocages
    "ramp_start": 15,
    "ramp_step": 10,
    "max_follows_per_hour": 20,
    "max_unfollows_per_hour": 25,
    "max_unfollows_per_day": 100,
    "max_following": 1000,
    # part du temps du jour au plus pour les desabonnements tant qu'il reste des abonnements a faire
    "unfollow_time_share": 0.5,
    # limitation silencieuse (TikTok ignore les abonnements sans message) : un abonnement recent est
    # verifie sur son profil tous les verify_every abonnements, et phantom_streak comptes de suite deja
    # plus suivis au moment du desabonnement valent un blocage (0 : desactive)
    "verify_every": 10,
    "phantom_streak": 5,
    "max_wait_minutes": 75,
    "pause_jitter": [0.6, 1.5],
    "long_pause_every": 10,
    "long_pause_seconds": [120, 300],
    "active_hours": [8, 23],
    "daily_minutes": 180,
    "keep_awake": True,
    "rest_day_chance": 0.15,
    "block_cooldown_hours": 48,
    "captcha_wait_minutes": 10,
    "captcha_cooldown_hours": 12,
    "warning_cooldown_hours": 168,
    # delai adaptatif : mesure du temps de follow-back, puis ajustement du desabonnement
    "adaptive_delay": True,
    "unfollow_days_min": 2,
    "unfollow_days_max": 7,
    "adapt_target_share": 0.9,
    "adapt_min_events": 25,
    "adapt_min_at_risk": 15,
    "adapt_window_days": 60,
    "explore_share": 0.2,
    "followback_scan_rows": 1000,
    "max_scrolls_per_seed": 80,
    "virtual_desktop": 2,
    "browser_channel": "msedge",
    "locale": "fr-FR",
    "timezone": "Europe/Paris",
    "debug_screenshots": True,
}


def norm_handles(items):
    return list(dict.fromkeys(h.strip().lstrip("@").lower() for h in items if h and h.strip()))


def read_seeds_file(path):
    return norm_handles(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


def load_config():
    cfg = dict(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    own = norm_handles(cfg.get("seeds") or [])
    seeds_file = ROOT / "seeds.txt"
    if not own and seeds_file.exists():
        cfg["seeds"], cfg["seeds_source"] = read_seeds_file(seeds_file), "seeds.txt"
    else:
        cfg["seeds"], cfg["seeds_source"] = own, "config.json"
    cfg["username"] = str(cfg["username"]).strip().lstrip("@").lower()
    return cfg


def setup_log():
    DATA.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rotate")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(DATA / "log.txt", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout is not None:  # pythonw : pas de console
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


log = setup_log()


def rnd(lo, hi):
    return random.uniform(lo, hi)


# ---------------------------------------------------------------------------
# Etat (SQLite) : qui a ete suivi, depuis quelle source, quand, et s'il a ete desuivi
# ---------------------------------------------------------------------------
def today():
    return date.today()


def now():
    return datetime.now()


# Note d'un abonnement que TikTok n'a jamais enregistre (bouton revenu sur Suivre) : sans follow-back, il
# ne compte pas dans les statistiques (le compte n'a jamais ete suivi, il n'a rien pu rendre).
PHANTOM = "non pris en compte"
REAL_FOLLOW = f"NOT (COALESCE(note, '') = '{PHANTOM}' AND followed_back_on IS NULL)"


class State:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS users ("
            " username TEXT PRIMARY KEY, followed_on TEXT NOT NULL, followed_at TEXT NOT NULL,"
            " unfollowed_on TEXT, note TEXT, followed_back_on TEXT, fixed_days INTEGER, unfollowed_at TEXT,"
            " source TEXT);"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        self.db.commit()

    def known(self, user):
        return self.db.execute("SELECT 1 FROM users WHERE username=?", (user,)).fetchone() is not None

    def add_follow(self, user, source=None, fixed_days=None):
        """fixed_days : delai propre a ce compte (groupe temoin) ; None = delai courant."""
        self.db.execute(
            "INSERT OR IGNORE INTO users (username, followed_on, followed_at, fixed_days, source) VALUES (?,?,?,?,?)",
            (user, today().isoformat(), now().isoformat(timespec="seconds"), fixed_days, source))
        self.db.commit()

    def mark_unfollowed(self, user, note=None, action=True):
        """action=False : rien n'a ete clique (compte disparu, abonnement absent) ; sans horodatage, cela ne
        compte ni dans le plafond horaire ni dans le plafond quotidien."""
        self.db.execute("UPDATE users SET unfollowed_on=?, unfollowed_at=?, note=? WHERE username=?",
                        (today().isoformat(), now().isoformat(timespec="seconds") if action else None, note, user))
        self.db.commit()

    def followed_at(self, user):
        row = self.db.execute("SELECT followed_at FROM users WHERE username=?", (user,)).fetchone()
        return row[0] if row else ""

    def mark_followed_back(self, user):
        self.db.execute("UPDATE users SET followed_back_on=? WHERE username=? AND followed_back_on IS NULL",
                        (today().isoformat(), user))
        self.db.commit()

    def followed_today(self):
        return self.count("followed_on=?", (today().isoformat(),))

    def unfollowed_today(self):
        return self.count("unfollowed_on=? AND unfollowed_at IS NOT NULL", (today().isoformat(),))

    def following(self, keep_followbacks=False):
        """Abonnements en cours faits par le script (hors follow-back gardes si keep_followbacks)."""
        return self.count("unfollowed_on IS NULL" + (" AND followed_back_on IS NULL" if keep_followbacks else ""))

    def active_follow_days(self):
        """Jours (avant aujourd'hui) ou des abonnements ont ete faits : sert a la montee en charge."""
        return self.db.execute("SELECT COUNT(DISTINCT followed_on) FROM users WHERE followed_on < ?",
                               (today().isoformat(),)).fetchone()[0]

    def actions_since(self, kind, since):
        """Horodatages (ISO) des abonnements ('follow') ou desabonnements ('unfollow') depuis `since`."""
        col = "followed_at" if kind == "follow" else "unfollowed_at"
        rows = self.db.execute(f"SELECT {col} FROM users WHERE {col} >= ?",
                               (since.isoformat(timespec="seconds"),)).fetchall()
        return [r[0] for r in rows]

    def due(self, days, limit, keep_followbacks=False, keep_days=0):
        """Comptes a desabonner : age >= delai propre au compte, sinon `days` (delai courant) ; ceux qui ont
        suivi en retour, pas avant `keep_days` jours. Ceux qui n'ont rien rendu passent en premier."""
        rows = self.db.execute(
            "SELECT username FROM users WHERE unfollowed_on IS NULL "
            + ("AND followed_back_on IS NULL " if keep_followbacks else "") +
            "AND date(followed_on, '+' || (CASE WHEN followed_back_on IS NULL THEN COALESCE(fixed_days, ?) "
            "     ELSE MAX(COALESCE(fixed_days, ?), ?) END) || ' days') <= ? "
            "ORDER BY followed_back_on IS NOT NULL, followed_on, username LIMIT ?",
            (days, days, keep_days, today().isoformat(), limit)).fetchall()
        return [r[0] for r in rows]

    def pending(self):
        """Comptes suivis depuis hier ou avant, toujours suivis, sans follow-back constate."""
        rows = self.db.execute(
            "SELECT username FROM users WHERE unfollowed_on IS NULL AND followed_back_on IS NULL "
            "AND followed_on < ?", (today().isoformat(),)).fetchall()
        return {r[0] for r in rows}

    def samples(self, window_days):
        """[(jours observes, a_suivi_en_retour)] pour les comptes suivis dans la fenetre."""
        since = (today() - timedelta(days=window_days)).isoformat()
        out = []
        for f, fb, uf in self.db.execute(
                "SELECT followed_on, followed_back_on, unfollowed_on FROM users WHERE followed_on>=? AND "
                + REAL_FOLLOW, (since,)):
            start = date.fromisoformat(f)
            if fb:
                out.append(((date.fromisoformat(fb) - start).days, True))
            else:  # pas (encore) de follow-back : observe jusqu'au desabonnement, ou jusqu'a aujourd'hui
                out.append((((date.fromisoformat(uf) if uf else today()) - start).days, False))
        return out

    def seed_table(self, score_days):
        """{source: {total, n, fb}} : total = comptes suivis ; n = ceux suivis il y a au moins `score_days`
        jours ; fb = parmi eux, ceux qui ont suivi en retour sous `score_days` jours. Meme fenetre pour
        toutes les sources, donc comparable quel que soit le delai de desabonnement de chaque compte."""
        cutoff = (today() - timedelta(days=score_days)).isoformat()
        rows = self.db.execute(
            "SELECT source, COUNT(*), "
            " SUM(CASE WHEN followed_on <= ? THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN followed_on <= ? AND followed_back_on IS NOT NULL "
            "      AND julianday(followed_back_on) - julianday(followed_on) <= ? THEN 1 ELSE 0 END) "
            "FROM users WHERE source IS NOT NULL AND " + REAL_FOLLOW + " GROUP BY source",
            (cutoff, cutoff, score_days)).fetchall()
        return {s: {"total": t, "n": n or 0, "fb": fb or 0} for s, t, n, fb in rows}

    def scored(self, score_days):
        """[(pseudo, followed_at, follow-back sous `score_days` jours)] pour les comptes suivis il y a au
        moins `score_days` jours : meme fenetre que seed_table, pour comparer des groupes de comptes."""
        cutoff = (today() - timedelta(days=score_days)).isoformat()
        rows = self.db.execute(
            "SELECT username, followed_at, followed_back_on IS NOT NULL "
            " AND julianday(followed_back_on) - julianday(followed_on) <= ? "
            "FROM users WHERE followed_on <= ? AND " + REAL_FOLLOW, (score_days, cutoff)).fetchall()
        return [(u, at, bool(fb)) for u, at, fb in rows]

    def count(self, where="1=1", args=()):
        return self.db.execute(f"SELECT COUNT(*) FROM users WHERE {where}", args).fetchone()[0]

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
        self.db.commit()


def km_cdf(samples, horizon):
    """Kaplan-Meier. samples : [(jours, a_suivi_en_retour)]. Retourne {t: (F(t), nb_a_risque)}, t = 1..horizon.
    Un compte desabonne sans follow-back est "censure" : on sait seulement qu'il n'avait pas suivi en
    retour avant son desabonnement. Les ignorer, ou les compter comme des echecs, fausserait le delai."""
    curve, surv = {}, 1.0
    for t in range(1, horizon + 1):
        at_risk = sum(1 for dur, _ in samples if dur >= t)
        events = sum(1 for dur, ev in samples if ev and dur == t)
        if at_risk:
            surv *= 1 - events / at_risk
        curve[t] = (1 - surv, at_risk)
    return curve


def followback_stats(samples, cfg, base):
    """Mesure le follow-back et propose le delai de desabonnement.

    delay = plus petit nombre de jours qui capte `adapt_target_share` des follow-back obtenus sur la
    fenetre la plus longue (unfollow_days_max), borne entre unfollow_days_min et unfollow_days_max.
    Tant que cette fenetre longue n'est pas observee sur assez de comptes, delay reste `base`."""
    lo = cfg["unfollow_days_min"]
    hi = max(cfg["unfollow_days_max"], base)
    out = {"delay": base, "events": sum(1 for _, ev in samples if ev), "rate": None,
           "mean": None, "median": None, "horizon": None, "adapted": False}
    curve = km_cdf(samples, hi)
    usable = [t for t, (_, n) in curve.items() if n >= cfg["adapt_min_at_risk"]]
    if not usable:
        return out
    horizon = max(usable)
    total = curve[horizon][0]
    if total <= 0:
        return out
    prev = mean = 0.0
    for t in range(1, horizon + 1):
        mean += t * (curve[t][0] - prev)
        prev = curve[t][0]
    out.update(rate=total, mean=mean / total, horizon=horizon,
               median=next(t for t in range(1, horizon + 1) if curve[t][0] >= 0.5 * total))
    if horizon == hi and out["events"] >= cfg["adapt_min_events"]:
        d = next(t for t in range(1, hi + 1) if curve[t][0] >= cfg["adapt_target_share"] * total)
        out["delay"] = max(lo, min(hi, d))
        out["adapted"] = True
    return out


def describe(stats):
    if stats["rate"] is None:
        return f"pas encore assez de donnees ({stats['events']} follow-back mesure(s))"
    return (f"{stats['rate'] * 100:.1f} % des comptes suivent en retour sous {stats['horizon']} jours, "
            f"delai moyen {stats['mean']:.1f} j, median {stats['median']} j ({stats['events']} mesures)")


# ---------------------------------------------------------------------------
# Choix des sources : celles dont les abonnes suivent le plus en retour passent en premier
# ---------------------------------------------------------------------------
def beta_scores(table, keys, strength):
    """{cle: (alpha, beta)} : loi Beta du taux de follow-back de chaque groupe de `table` ({cle: {n, fb}}).
    L'a priori est le taux global, avec un poids de `strength` comptes : un groupe peu teste reste proche
    de la moyenne, un groupe bien mesure s'en ecarte selon ses resultats."""
    n_all = sum(v["n"] for v in table.values())
    fb_all = sum(v["fb"] for v in table.values())
    prior = (fb_all + 1) / (n_all + 2)
    k = max(1.0, float(strength))
    out = {}
    for s in keys:
        v = table.get(s, {"n": 0, "fb": 0})
        out[s] = (k * prior + v["fb"], k * (1 - prior) + v["n"] - v["fb"])
    return out


def seed_scores(st, cfg, seeds):
    """{source: (alpha, beta)} : loi Beta du taux de follow-back de chaque source (voir beta_scores)."""
    return beta_scores(st.seed_table(cfg["seed_score_days"]), seeds, cfg["seed_prior_strength"])


def segment_table(rows, key):
    """{groupe: {n, fb}} a partir de State.scored, groupe = key(pseudo, followed_at)."""
    out = {}
    for user, at, fb in rows:
        v = out.setdefault(key(user, at), {"n": 0, "fb": 0})
        v["n"] += 1
        v["fb"] += fb
    return out


def handle_kind(user, at=None):
    return "generique" if GENERIC_HANDLE_RE.match(user) else "perso"


def day_part(user, at):
    hour = int(at[11:13])
    return "matin (avant 12 h)" if hour < 12 else "apres-midi (12-18 h)" if hour < 18 else "soir (apres 18 h)"


def handle_filter(st, cfg):
    """Fonction pseudo -> True s'il vaut un abonnement. Les pseudos generiques sont compares aux autres sur
    la meme fenetre que les sources. Tant que chaque groupe n'a pas handle_filter_min comptes mesures, tout
    est suivi. Ensuite, pour chaque pseudo generique, on tire un taux plausible pour chaque groupe (Thompson)
    et on ne le suit que si le sien atteint handle_filter_ratio fois celui des autres. Ecarter un compte ne
    coute qu'un peu de defilement, un abonnement coute plusieurs minutes du plafond horaire.
    handle_filter_explore des pseudos generiques sont suivis quoi qu'il arrive, pour continuer a mesurer."""
    if not cfg["handle_filter"]:
        return lambda user: True
    table = segment_table(st.scored(cfg["seed_score_days"]), handle_kind)
    if min(table.get(k, {"n": 0})["n"] for k in ("generique", "perso")) < cfg["handle_filter_min"]:
        return lambda user: True
    scores = beta_scores(table, ["generique", "perso"], cfg["seed_prior_strength"])

    def wanted(user):
        if handle_kind(user) == "perso" or random.random() < cfg["handle_filter_explore"]:
            return True
        return random.betavariate(*scores["generique"]) >= cfg["handle_filter_ratio"] * random.betavariate(
            *scores["perso"])
    return wanted


def seed_rotation(seeds, last):
    """Sources dans l'ordre de seeds.txt, en commencant par celle qui suit la derniere utilisee."""
    start = seeds.index(last) + 1 if last in seeds else 0
    return seeds[start:] + seeds[:start]


def seed_order(st, cfg, seeds):
    """Ordre d'essai des sources pour ce passage.

    seed_scoring : echantillonnage de Thompson. Pour chaque source on tire un taux plausible dans sa loi
    Beta et on trie par taux tire. La meilleure source passe le plus souvent en tete, mais une source peu
    testee a encore sa chance (exploration). Sinon : rotation simple. Dans les deux cas, une source
    epuisee recemment (plus aucun nouveau compte) passe en dernier pendant seed_dry_days jours."""
    if cfg["seed_scoring"]:
        scores = seed_scores(st, cfg, seeds)
        draw = {s: random.betavariate(a, b) for s, (a, b) in scores.items()}
        order = sorted(seeds, key=lambda s: -draw[s])
    else:
        order = seed_rotation(seeds, st.get_meta("last_seed"))
    day = today().isoformat()

    def dry(s):
        until = st.get_meta(f"seed_dry_until:{s}")
        return bool(until and until > day)
    return [s for s in order if not dry(s)] + [s for s in order if dry(s)]


# ---------------------------------------------------------------------------
# Prudence : montee en charge, plafonds horaires, plage horaire, repos, blocages
# ---------------------------------------------------------------------------
BLOCK_POLICY = {  # reaction : (cle de config de la pause, coefficient applique aux quotas)
    "limit": ("block_cooldown_hours", 0.5),
    "captcha": ("captcha_cooldown_hours", 0.75),
    "warning": ("warning_cooldown_hours", 0.25),
}


def is_rest_day(cfg, day):
    """Jour de repos : tire une fois par date (stable d'un passage a l'autre), jamais deux jours de suite."""
    def drawn(d):
        return random.Random("repos-" + d.isoformat()).random() < cfg["rest_day_chance"]
    return drawn(day) and not drawn(day - timedelta(days=1))


def in_active_hours(cfg, moment):
    lo, hi = cfg["active_hours"]
    return lo <= moment.hour < hi


def follow_budget(st, cfg):
    """Abonnements autorises aujourd'hui : montee en charge progressive, reduite apres un blocage."""
    ramp = cfg["ramp_start"] + cfg["ramp_step"] * st.active_follow_days()
    factor = float(st.get_meta("quota_factor") or 1.0)
    return max(0, int(min(cfg["follows_per_day"], ramp) * factor))


def register_block(st, cfg, kind):
    """Reaction de TikTok : quotas reduits, pause qui double a chaque blocage rapproche (14 jours max)."""
    key, mult = BLOCK_POLICY.get(kind, BLOCK_POLICY["limit"])
    streak = int(st.get_meta("block_streak") or 0) + 1
    factor = max(0.25, float(st.get_meta("quota_factor") or 1.0) * mult)
    hours = min(cfg[key] * 2 ** (streak - 1), 14 * 24)
    st.set_meta("block_streak", str(streak))
    st.set_meta("quota_factor", f"{factor:.2f}")
    st.set_meta("blocked_until", (now() + timedelta(hours=hours)).isoformat(timespec="seconds"))
    st.set_meta("last_block_at", now().isoformat(timespec="seconds"))
    st.set_meta("last_clean_on", today().isoformat())  # pas de remontee le jour meme
    return hours, factor


def register_clean_day(st):
    """Un jour sans reaction de TikTok : les quotas remontent de 10 points, jusqu'a 100 %."""
    if st.get_meta("last_clean_on") == today().isoformat():
        return
    st.set_meta("last_clean_on", today().isoformat())
    factor = min(1.0, float(st.get_meta("quota_factor") or 1.0) + 0.1)
    st.set_meta("quota_factor", f"{factor:.2f}")
    if factor >= 1.0:
        st.set_meta("block_streak", "0")


def minutes_left(st, cfg):
    """Temps de passage restant aujourd'hui, cumule sur tous les passages (None = illimite)."""
    if not cfg["daily_minutes"]:
        return None
    used = float(st.get_meta(f"run_seconds:{today().isoformat()}") or 0)
    return max(0.0, cfg["daily_minutes"] - used / 60)


def add_run_time(st, started):
    key = f"run_seconds:{today().isoformat()}"
    st.set_meta(key, str(float(st.get_meta(key) or 0) + (now() - started).total_seconds()))


def keep_awake(on):
    """Windows : empeche la mise en veille pendant le passage (l'ecran peut s'eteindre)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes, fn.restype = [ctypes.c_uint], ctypes.c_uint
        fn(0x80000000 | (0x00000001 if on else 0))  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    except Exception:
        pass


def day_over(cfg):
    """True si la plage horaire est finie ou le temps de passage du jour ecoule."""
    moment, deadline = now(), cfg.get("_deadline")
    return not in_active_hours(cfg, moment) or bool(deadline and moment >= deadline)


def unfollow_until(cfg, quota):
    """Fin de la part du temps reservee aux desabonnements (None : sans limite). Sans elle, un retard de
    desabonnements (apres une pause) prendrait tout le temps du jour et aucun abonnement ne se ferait."""
    deadline, share = cfg.get("_deadline"), cfg["unfollow_time_share"]
    if not (quota and deadline) or share >= 1:
        return None
    return now() + max(timedelta(0), deadline - now()) * share


def throttle(page, st, cfg, kind):
    """Respecte le plafond horaire (cumule sur tous les passages), la plage horaire et le temps de
    passage du jour. Retourne False s'il faut s'arreter pour aujourd'hui."""
    limit = cfg[f"max_{kind}s_per_hour"]
    deadline = cfg.get("_deadline")
    while True:
        moment = now()
        if not in_active_hours(cfg, moment):
            log.info("Hors de la plage horaire %dh-%dh : arret, reprise au prochain passage", *cfg["active_hours"])
            return False
        if deadline and moment >= deadline:
            log.info("Temps de passage du jour ecoule (%d min) : arret, reprise au prochain passage", cfg["daily_minutes"])
            return False
        recent = st.actions_since(kind, moment - timedelta(hours=1))
        if len(recent) < limit:
            return True
        wait = (datetime.fromisoformat(min(recent)) + timedelta(hours=1) - moment).total_seconds() + rnd(5, 90)
        if wait > cfg["max_wait_minutes"] * 60 or (deadline and moment + timedelta(seconds=wait) >= deadline):
            log.info("Plafond horaire atteint et attente trop longue : arret pour aujourd'hui")
            return False
        log.info("Plafond horaire atteint (%d %s par heure) : attente de %d min", limit, kind, wait // 60)
        page.wait_for_timeout(wait * 1000)


def acquire_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            if pid != os.getpid() and psutil.pid_exists(pid):
                return False
        except ValueError:
            pass
    LOCK.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK.unlink(missing_ok=True))
    return True


# ---------------------------------------------------------------------------
# Navigateur
# ---------------------------------------------------------------------------
def launch(p, cfg, hidden):
    args = [
        "--window-size=1280,900",
        # une fenetre cachee / sur un autre bureau ne doit pas etre ralentie
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    if hidden:
        args.append("--window-position=-32000,-32000")  # hors ecran jusqu'au placement
    kw = dict(user_data_dir=str(PROFILE), headless=False, args=args, no_viewport=True,
              locale=cfg["locale"], timezone_id=cfg["timezone"])
    channel = cfg.get("browser_channel")
    try:
        return p.chromium.launch_persistent_context(channel=channel, **kw)
    except Exception as e:
        if not channel:
            raise
        log.warning("Navigateur '%s' indisponible (%s), essai avec le Chromium de Playwright",
                    channel, str(e).splitlines()[0][:120])
        return p.chromium.launch_persistent_context(**kw)


def place_on_bot_desktop(desktop_no):
    """Windows : envoie la fenetre du navigateur sur le bureau virtuel dedie (cree si besoin)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        from pyvda import AppView, VirtualDesktop, get_virtual_desktops
    except Exception as e:
        log.warning("Bureau virtuel indisponible (%s) : la fenetre reste hors ecran", e)
        return False

    user32 = ctypes.windll.user32
    target = str(PROFILE.resolve()).lower()
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def find_hwnd():
        pids = set()
        for pr in psutil.process_iter(["pid", "cmdline"]):
            try:
                if target in " ".join(pr.info["cmdline"] or []).lower():
                    pids.add(pr.info["pid"])
            except psutil.Error:
                continue
        best = [None, 0]

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    area = (r.right - r.left) * (r.bottom - r.top)
                    if area > best[1]:
                        best[0], best[1] = hwnd, area
            return True

        user32.EnumWindows(enum_proc(cb), 0)
        return best[0]

    hwnd = None
    for _ in range(40):  # ~20 s
        hwnd = find_hwnd()
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        log.warning("Fenetre du navigateur introuvable : elle reste hors ecran")
        return False
    try:
        total = len(get_virtual_desktops())
        while total < desktop_no:
            VirtualDesktop.create()
            total += 1
            try:
                VirtualDesktop(total).rename("TikTok")
            except Exception:
                pass
        for _ in range(5):
            try:
                AppView(hwnd=hwnd).move(VirtualDesktop(desktop_no))
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("deplacement refuse par Windows")
        # une fois sur l'autre bureau, on la remet a une position visible (invisible pour toi)
        user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(0), 60, 40, 0, 0,
                            0x0001 | 0x0004 | 0x0010)  # NOSIZE | NOZORDER | NOACTIVATE
    except Exception as e:
        log.warning("Placement sur le bureau %d impossible (%s) : fenetre hors ecran", desktop_no, e)
        return False
    log.info("Fenetre placee sur le bureau virtuel %d", desktop_no)
    return True


def visible(loc):
    """True si au moins un element du locator est visible (pas seulement le premier du DOM)."""
    try:
        return loc.filter(visible=True).count() > 0
    except Exception:
        return False


def dismiss_popups(page):
    for rx in (COOKIES_RE, LATER_RE):
        try:
            btn = page.get_by_role("button", name=rx).filter(visible=True).first
            if btn.count():
                btn.click(timeout=3000)
                page.wait_for_timeout(800)
        except Exception:
            pass


def captcha_visible(page):
    return visible(page.locator(CAPTCHA_SEL)) or visible(page.get_by_text(CAPTCHA_RE))


def block_kind(page):
    """None, 'limit' (rythme trop rapide), 'captcha' (verification anti-robot), 'warning' (automatisation
    soupconnee) ou 'attention' (compte banni ou suspendu, deconnexion)."""
    try:
        if ATTENTION_URL_RE.search(page.url) or visible(page.locator(LOGIN_SEL)):
            return "attention"
        if visible(page.get_by_text(ATTENTION_RE)):
            return "attention"
        if captcha_visible(page):
            return "captcha"
        for kind, rx in (("warning", WARN_RE), ("limit", BLOCK_RE)):
            if visible(page.get_by_text(rx)):
                return kind
    except Exception:
        pass
    return None


def wait_captcha(page, cfg):
    """Captcha : on laisse le temps de le resoudre a la main dans la fenetre du bot (bureau virtuel).
    Le script ne tente jamais de le resoudre lui-meme. Retourne True s'il a disparu."""
    minutes = cfg["captcha_wait_minutes"]
    if not minutes:
        return False
    log.warning("Captcha TikTok : resous-le dans la fenetre du bot (bureau virtuel %d), attente %d min au plus",
                cfg["virtual_desktop"], minutes)
    shot(page, cfg, "captcha")
    end = time.monotonic() + minutes * 60
    while time.monotonic() < end:
        page.wait_for_timeout(5000)
        if not captcha_visible(page):
            log.info("Captcha resolu : reprise")
            page.wait_for_timeout(rnd(2000, 4000))
            return True
    return False


def blocked(page, cfg):
    """True si TikTok reagit ; la nature de la reaction est gardee dans cfg['_block']."""
    kind = block_kind(page)
    if kind == "captcha" and wait_captcha(page, cfg):
        kind = block_kind(page)
    if kind:
        cfg["_block"] = kind
    return kind is not None


def prune(folder, pattern, keep):
    for old in sorted(folder.glob(pattern))[:-keep]:
        old.unlink(missing_ok=True)


def shot(page, cfg, name, force=False):
    if not (force or cfg.get("debug_screenshots")):
        return
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"))
        prune(SHOTS, "*.png", 30)
    except Exception:
        pass


def detect_username(page):
    """Pseudo du compte connecte, lu dans le lien Profil du menu."""
    try:
        href = page.locator(NAV_PROFILE_SEL).first.get_attribute("href", timeout=5000) or ""
    except Exception:
        return ""
    m = re.search(r"/@([\w.]+)", href)
    return m.group(1).lower() if m else ""


def logged_in(page):
    page.goto(TT + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(f"{LOGGED_IN_SEL}, {LOGIN_SEL}", timeout=20000)
    except PWTimeout:
        pass
    dismiss_popups(page)
    page.wait_for_timeout(1500)
    if ATTENTION_URL_RE.search(page.url) or visible(page.locator(LOGIN_SEL)):
        return False
    return page.locator(LOGGED_IN_SEL).count() > 0


def human_pause(page, cfg, n, kind):
    """Pause entre deux actions : etalee sur l'heure (3600 s / plafond horaire), avec variation."""
    secs = 3600 / cfg[f"max_{kind}s_per_hour"] * rnd(*cfg["pause_jitter"])
    if n % cfg["long_pause_every"] == 0:
        secs += rnd(*cfg["long_pause_seconds"])
    deadline = cfg.get("_deadline")
    if deadline:  # inutile de dormir au-dela du temps de passage
        secs = min(secs, max(0.0, (deadline - now()).total_seconds()))
    page.wait_for_timeout(secs * 1000)


# ---------------------------------------------------------------------------
# Profils et listes d'abonnes
# ---------------------------------------------------------------------------
def open_profile(page, user):
    page.goto(f"{TT}/@{user}", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(f"{USER_TITLE_SEL}, {FOLLOWERS_COUNT_SEL}", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(rnd(2000, 3500))
    dismiss_popups(page)


def profile_state(page):
    """'follow' (pas abonne), 'following' (abonne, ami ou demande envoyee) ou None (bouton introuvable).
    Le bouton trouve est marque : page.locator(PROFILE_BTN_SEL)."""
    pattern = f"(?:{NOT_FOLLOWING_RE.pattern})|(?:{FOLLOWING_RE.pattern})"
    text = page.evaluate(JS_PROFILE_BTN, {"title": USER_TITLE_SEL, "re": pattern})
    if text is None:
        return None
    return "follow" if NOT_FOLLOWING_RE.match(text) else "following"


def open_followers(page, user, cfg, load=True):
    """Ouvre la liste d'abonnes de `user`. Retourne (fenetre, 'ok' | 'blocked' | 'missing' | 'hidden')."""
    if load:
        open_profile(page, user)
    if blocked(page, cfg):
        return None, "blocked"
    if visible(page.get_by_text(NOT_FOUND_RE)):
        return None, "missing"
    try:
        page.locator(FOLLOWERS_COUNT_SEL).or_(page.locator(FOLLOWERS_LABEL_SEL)).first.click(timeout=8000)
    except PWTimeout:
        return None, "hidden"
    page.wait_for_timeout(rnd(1500, 2500))
    try:  # la fenetre a des onglets (Abonnements, Followers, Amis...) : on s'assure d'etre sur Followers
        tab = page.get_by_role("tab", name=FOLLOWERS_TAB_RE).filter(visible=True).first
        if tab.count() and tab.get_attribute("aria-selected") != "true":
            tab.click(timeout=3000)
            page.wait_for_timeout(rnd(1200, 2000))
    except Exception:
        pass
    dlg = page.locator(DIALOG_SEL).filter(has=page.locator(ROW_LINK_SEL)).last
    try:
        dlg.wait_for(state="visible", timeout=12000)
    except PWTimeout:
        return None, ("blocked" if blocked(page, cfg) else "hidden")
    page.wait_for_timeout(rnd(1500, 2500))
    return dlg, "ok"


def list_users(dlg):
    return {u for u in dlg.locator(ROW_LINK_SEL).evaluate_all(JS_HREF_USERS) if u}


def scroll_dialog(page, dlg):
    """Molette au-dessus de la liste ; si elle n'a pas bouge (curseur hors de la zone defilante),
    defilement direct de l'element defilant."""
    try:
        pos = dlg.evaluate(JS_SCROLLER, 0)
    except Exception:
        pos = None
    box = dlg.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.6)
    page.mouse.wheel(0, random.randint(500, 900))
    page.wait_for_timeout(rnd(1800, 3200))
    try:
        if pos is not None and dlg.evaluate(JS_SCROLLER, 0) == pos:
            dlg.evaluate(JS_SCROLLER, random.randint(500, 900))
            page.wait_for_timeout(rnd(1200, 2000))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Abonnements
# ---------------------------------------------------------------------------
def follow_from_seed(page, seed, quota, st, cfg, counter, fixed_days=lambda: None, wanted=lambda u: True):
    """Suit jusqu'a `quota` abonnes de `seed` (bouton "Suivre" seulement : ceux qui te suivent deja
    affichent "Suivre en retour" et sont ignores ; `wanted` ecarte ceux qui rapportent trop peu). Retourne
    (nombre, 'ok' | 'dry' | 'skip' | 'stop' | 'blocked') ; 'dry' = plus aucun compte a suivre dans cette liste."""
    dlg, res = open_followers(page, seed, cfg)
    if dlg is None:
        if res == "blocked":
            shot(page, cfg, f"blocked-{seed}")
            return 0, "blocked"
        log.info("[%s] liste d'abonnes inaccessible (%s), source ignoree", seed,
                 "compte introuvable" if res == "missing" else "liste masquee ou interface modifiee")
        shot(page, cfg, f"followers-{seed}")
        return 0, "skip"
    me = cfg["username"]
    recent = cfg.setdefault("_recent", [])
    done = fails = scrolls = stalls = filtered = 0
    skip, rows = set(), set()
    try:
        while done < quota:
            btns = dlg.get_by_role("button", name=FOLLOW_RE)
            users = btns.evaluate_all(JS_USERS)
            idx = next((i for i, u in enumerate(users)
                        if u and u != me and u not in skip and not st.known(u)), None)
            if idx is None:  # rien d'eligible a l'ecran : on descend dans la liste
                if scrolls >= cfg["max_scrolls_per_seed"]:
                    return done, "dry"
                scroll_dialog(page, dlg)
                scrolls += 1
                if blocked(page, cfg):
                    shot(page, cfg, "blocked-scroll")
                    return done, "blocked"
                now_rows = list_users(dlg)
                stalls = 0 if now_rows - rows else stalls + 1
                rows |= now_rows
                if stalls >= 3:  # fin de liste
                    return done, "dry"
                continue
            user = users[idx]
            if not wanted(user):
                skip.add(user)
                filtered += 1
                continue
            if not throttle(page, st, cfg, "follow"):
                return done, "stop"
            try:
                btns.nth(idx).click(timeout=8000)
            except PWTimeout:
                skip.add(user)
                continue
            page.wait_for_timeout(rnd(1500, 2500))
            if blocked(page, cfg):
                shot(page, cfg, "blocked-follow")
                return done, "blocked"
            if not follow_registered(page, dlg, user):
                # le bouton est reste ou revenu sur "Suivre" : TikTok a ignore le clic (limitation silencieuse)
                fails += 1
                skip.add(user)
                log.info("[%s] abonnement a @%s non pris en compte (%d de suite)", seed, user, fails)
                shot(page, cfg, f"follow-{user}")
                if fails >= 3:
                    cfg["_block"] = "limit"
                    return done, "blocked"
                continue
            fails = 0
            st.add_follow(user, seed, fixed_days())
            done += 1
            counter[0] += 1
            recent.append(user)
            log.info("[%s] abonne a @%s (%d/%d)", seed, user, done, quota)
            if cfg["verify_every"] and len(recent) % cfg["verify_every"] == 0:
                if verify_recent(page, st, cfg) == "blocked":
                    return done, "blocked"
            human_pause(page, cfg, counter[0], "follow")
        return done, "ok"
    finally:
        if filtered:
            log.info("[%s] %d pseudo(s) generique(s) ecarte(s) (follow-back mesure trop faible)", seed, filtered)


def follow_registered(page, dlg, user):
    """True quand le bouton de la ligne est passe a Abonne / Amis / Demande ; TikTok met parfois plusieurs
    secondes. False seulement s'il est encore sur Suivre apres ~10 s."""
    for _ in range(5):
        if user in dlg.get_by_role("button", name=FOLLOWING_RE).evaluate_all(JS_USERS):
            return True
        page.wait_for_timeout(2000)
    return user not in dlg.get_by_role("button", name=FOLLOW_RE).evaluate_all(JS_USERS)


def follow_sticks(page, user, cfg):
    """Ouvre le profil de `user` dans un second onglet (la liste en cours reste ouverte). Retourne 'ok' si
    l'abonnement y figure, 'lost' si le bouton est sur Suivre, 'blocked' si TikTok reagit, None si on ne
    peut pas conclure."""
    tab = page.context.new_page()
    try:
        open_profile(tab, user)
        if blocked(tab, cfg):
            shot(tab, cfg, "blocked-verify")
            return "blocked"
        if visible(tab.get_by_text(NOT_FOUND_RE)):
            return None
        state = profile_state(tab)
        return None if state is None else ("ok" if state == "following" else "lost")
    except Exception:  # sondage facultatif : une erreur de navigation ne doit pas arreter le passage
        return None
    finally:
        try:
            tab.close()
            page.bring_to_front()
        except Exception:
            pass


def verify_recent(page, st, cfg):
    """Sondage contre la limitation silencieuse : le bouton de la liste peut afficher "Abonne" alors que
    TikTok n'a rien enregistre. Un abonnement recent du passage (pas le dernier, laisse le temps a TikTok)
    est verifie sur le profil du compte ; s'il a disparu, un second est verifie. Deux disparus = TikTok
    ignore les abonnements : on s'arrete avant que la restriction ne s'aggrave. Retourne 'ok' ou 'blocked'."""
    checked = cfg.setdefault("_checked", set())
    pool = [u for u in cfg["_recent"][-cfg["verify_every"]:-1] if u not in checked]
    random.shuffle(pool)
    lost = 0
    for user in pool[:2]:
        checked.add(user)
        res = follow_sticks(page, user, cfg)
        if res == "blocked":
            return "blocked"
        if res != "lost":
            return "ok"
        lost += 1
        st.mark_unfollowed(user, PHANTOM, action=False)
        log.warning("Abonnement a @%s absent de son profil : TikTok ne l'a pas enregistre", user)
    if lost >= 2:
        log.warning("Deux abonnements recents ignores par TikTok : limitation silencieuse probable")
        cfg["_block"] = "limit"
        return "blocked"
    return "ok"


def follow_pass(page, st, cfg, counter, seeds, quota, fixed_days):
    """Abonnements du passage, par tranches de follows_per_seed. Apres chaque tranche la source est tiree a
    nouveau : la meme peut revenir, et sa liste rouverte montre alors ses abonnes les plus recents, en ligne
    a cet instant (ils voient la notification tout de suite). Les tirages repartis sur plusieurs sources
    font aussi apprendre plus vite laquelle rend le mieux. Une source epuisee ou inaccessible est ecartee
    pour le passage, seeds_per_run fois au plus. Retourne (nombre, 'ok' | 'stop' | 'blocked')."""
    remaining, follows, misses, out = quota, 0, 0, set()
    wanted = handle_filter(st, cfg)
    while remaining > 0 and misses < cfg["seeds_per_run"]:
        pool = [s for s in seed_order(st, cfg, seeds) if s not in out]
        if not pool:
            break
        seed = pool[0]
        if cfg["seed_scoring"]:
            a, b = seed_scores(st, cfg, [seed])[seed]
            log.info("[%s] source choisie (follow-back estime %.1f %%)", seed, a / (a + b) * 100)
        n, s = follow_from_seed(page, seed, min(remaining, cfg["follows_per_seed"] or remaining), st, cfg,
                                counter, fixed_days, wanted)
        follows += n
        remaining -= n
        if s == "skip":  # source inaccessible : ecartee apres 3 echecs
            st.set_meta(f"seed_fail:{seed}", str(int(st.get_meta(f"seed_fail:{seed}") or 0) + 1))
            out.add(seed)
            misses += 1
            continue
        st.set_meta(f"seed_fail:{seed}", "0")
        st.set_meta("last_seed", seed)
        if s in ("blocked", "stop"):
            return follows, s
        if s == "dry":
            st.set_meta(f"seed_dry_until:{seed}", (today() + timedelta(days=cfg["seed_dry_days"])).isoformat())
            out.add(seed)
            misses += 1
            log.info("[%s] plus de compte a suivre : source suivante", seed)
    if remaining > 0:
        log.info("Quota non atteint : il manque %d abonnement(s)", remaining)
    return follows, "ok"


# ---------------------------------------------------------------------------
# Follow-back : detection dans la liste de tes abonnes
# ---------------------------------------------------------------------------
def scan_my_followers(page, me, st, cfg):
    """Parcourt la liste de TES abonnes et marque les comptes suivis qui t'ont suivi en retour.
    L'ordre de cette liste n'est pas garanti chronologique : on la lit en entier (plafonnee).
    Retourne ('ok' | 'skip' | 'blocked', lignes lues, follow-back trouves)."""
    pending = st.pending()
    if not pending:
        return "ok", 0, 0
    dlg, res = open_followers(page, me, cfg)
    if dlg is None:
        if res == "blocked":
            shot(page, cfg, "blocked-scan")
            return "blocked", 0, 0
        log.warning("Liste de tes abonnes inaccessible (pseudo '%s' correct ?) : mesure du jour ignoree", me)
        shot(page, cfg, "scan-followers")
        return "skip", 0, 0
    cap = cfg["followback_scan_rows"]
    seen, stalls, scrolls = set(), 0, 0
    while True:
        before = len(seen)
        seen |= list_users(dlg) - {me}
        if blocked(page, cfg):
            shot(page, cfg, "blocked-scan")
            return "blocked", len(seen), 0
        if pending <= seen or len(seen) >= cap or scrolls >= 600:
            break
        stalls = stalls + 1 if scrolls and len(seen) == before else 0
        if stalls >= 3:  # fin de liste
            break
        scroll_dialog(page, dlg)
        scrolls += 1
    found = pending & seen
    for user in found:
        st.mark_followed_back(user)
    if len(seen) >= cap and not pending <= seen:
        log.warning("Liste de tes abonnes tronquee a %d lignes : des follow-back ont pu etre manques", len(seen))
    log.info("Liste de tes abonnes : %d lignes lues, %d nouveau(x) follow-back sur %d en attente",
             len(seen), len(found), len(pending))
    return "ok", len(seen), len(found)


# ---------------------------------------------------------------------------
# Desabonnements
# ---------------------------------------------------------------------------
def unfollow_one(page, user, cfg):
    """Retourne 'ok' | 'gone' | 'already' | 'failed' | 'blocked' ; 'already' = deja plus abonne."""
    open_profile(page, user)
    if blocked(page, cfg):
        return "blocked"
    if visible(page.get_by_text(NOT_FOUND_RE)):
        return "gone"
    state = profile_state(page)
    if state == "follow":
        return "already"
    if state is None:
        shot(page, cfg, f"unfollow-{user}-nobutton")
        return "failed"
    try:
        page.locator(PROFILE_BTN_SEL).first.click(timeout=8000)
    except PWTimeout:
        shot(page, cfg, f"unfollow-{user}-click")
        return "failed"
    page.wait_for_timeout(rnd(1000, 1800))
    for _ in range(2):  # eventuel menu ou confirmation
        item = page.get_by_text(UNFOLLOW_RE).filter(visible=True).last
        if not item.count():
            break
        try:
            item.click(timeout=5000)
        except PWTimeout:
            break
        page.wait_for_timeout(rnd(1200, 2000))
    if blocked(page, cfg):
        return "blocked"
    for _ in range(4):
        if profile_state(page) == "follow":
            return "ok"
        page.wait_for_timeout(1500)
    shot(page, cfg, f"unfollow-{user}")
    return "failed"


def unfollow_pass(page, st, cfg, counter, days, until=None):
    """Desabonne les comptes arrives a echeance ; `until` : fin de la part du temps qui leur est reservee.
    Retourne (nombre, 'ok' | 'later' | 'stop' | 'blocked' | 'broken')."""
    due = st.due(days, max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today()), cfg["keep_followbacks"],
                 cfg["followback_keep_days"])
    log.info("%d compte(s) a desabonner", len(due))
    done = fails = phantoms = 0
    since = st.get_meta("last_block_at") or ""  # les abonnements d'avant ont deja valu un blocage
    for user in due:
        if until and now() >= until:
            log.info("Part du temps reservee aux desabonnements utilisee : place aux abonnements")
            return done, "later"
        if not throttle(page, st, cfg, "unfollow"):
            return done, "stop"
        res = unfollow_one(page, user, cfg)
        if res == "blocked":
            return done, "blocked"
        if res == "ok":
            st.mark_unfollowed(user)
            done += 1
            fails = phantoms = 0
            counter[0] += 1
            log.info("desabonne de @%s", user)
            human_pause(page, cfg, counter[0], "unfollow")
        elif res in ("gone", "already"):  # rien a cliquer : ni pause ni decompte dans les plafonds
            st.mark_unfollowed(user, "introuvable" if res == "gone" else PHANTOM, action=False)
            fails = 0
            if res == "gone":
                log.info("@%s introuvable : retire de la rotation", user)
                continue
            if st.followed_at(user) <= since:
                log.info("@%s n'etait deja plus suivi (abonnement d'avant la derniere pause)", user)
                continue
            phantoms += 1
            log.info("@%s n'etait deja plus suivi : abonnement non enregistre par TikTok ? (%d de suite)",
                     user, phantoms)
            if cfg["phantom_streak"] and phantoms >= cfg["phantom_streak"]:
                log.warning("%d abonnements de suite absents : TikTok les a ignores sans message "
                            "(limitation silencieuse probable)", phantoms)
                cfg["_block"] = "limit"
                return done, "blocked"
        else:
            fails += 1
            log.warning("echec du desabonnement de @%s (%d de suite)", user, fails)
            if fails >= 3:
                log.warning("3 echecs de suite : interface modifiee ? lance 'tiktok_rotate.py diagnose', "
                            "voir data/debug et la skill maj-selecteurs")
                return done, "broken"
    return done, "ok"


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
def save_config_key(key, value):
    path = ROOT / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remember_username(page, cfg):
    """Remplit la cle 'username' de config.json a partir du compte connecte, si elle est vide."""
    if cfg["username"]:
        return
    name = detect_username(page)
    if name:
        cfg["username"] = name
        save_config_key("username", name)
        log.info("Pseudo '%s' detecte et enregistre dans config.json", name)


def cmd_login(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch(p, cfg, hidden=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{TT}/login")
        input("Connecte-toi dans la fenetre (code de verification et captcha compris).\n"
              "Quand ton fil Pour toi s'affiche, reviens ici et appuie sur Entree... ")
        ok = logged_in(page)
        log.info("Session %s", "enregistree" if ok else "NON detectee : relance la commande login")
        if ok:
            st = State(DB_PATH)
            st.set_meta("needs_attention", "")
            remember_username(page, cfg)
            if not cfg["username"]:
                name = input("Ton pseudo TikTok, sans @ (sert a mesurer les follow-back) : ").strip().lstrip("@").lower()
                if name:
                    save_config_key("username", name)
                    log.info("Pseudo '%s' enregistre dans config.json", name)
        ctx.close()
    return 0 if ok else 1


def cmd_status(cfg):
    st = State(DB_PATH)
    base = cfg["unfollow_after_days"]
    days = int(st.get_meta("current_days") or base) if cfg["adaptive_delay"] else base
    keep, keep_days = cfg["keep_followbacks"], cfg["followback_keep_days"]
    stats = followback_stats(st.samples(cfg["adapt_window_days"]), cfg, base)
    factor = float(st.get_meta("quota_factor") or 1.0)
    kept = st.count("unfollowed_on IS NULL AND followed_back_on IS NOT NULL")
    print(f"Abonnements en cours (faits par le script) : {st.count('unfollowed_on IS NULL')}"
          + (f" dont {kept} follow-back gardes" + ("" if keep else f" ({keep_days} j au moins)") if kept else ""))
    print(f"A desabonner maintenant                    : {len(st.due(days, 10**6, keep, keep_days))}")
    print(f"Abonnements aujourd'hui                    : {st.followed_today()} / {follow_budget(st, cfg)} autorises"
          f" (plafond {cfg['follows_per_day']}, coefficient {factor:.2f})")
    print(f"Total historique                           : {st.count()}")
    used = float(st.get_meta(f"run_seconds:{today().isoformat()}") or 0) / 60
    print(f"Temps de passage aujourd'hui               : {used:.0f} / {cfg['daily_minutes'] or 'illimite'} min"
          f" (rythme : {cfg['max_follows_per_hour']} abonnements ou {cfg['max_unfollows_per_hour']} desabonnements par heure)")
    print(f"Delai de desabonnement actuel              : {days} jours (base {base})")
    print(f"Follow-back                                : {describe(stats)}")
    print(f"Sources                                    : {len(cfg['seeds'])} comptes ({cfg['seeds_source']}),"
          f" choix {'par taux de follow-back' if cfg['seed_scoring'] else 'en rotation'}")
    print(f"Pause anti-blocage jusqu'au                : {st.get_meta('blocked_until') or '-'}")
    month = (today() - timedelta(days=30)).isoformat()
    print(f"Abonnements ignores par TikTok (30 j)      : {st.count('note=? AND followed_on>=?', (PHANTOM, month))}"
          " (limitation silencieuse : bouton revenu sur Suivre)")
    if is_rest_day(cfg, today()):
        print("Aujourd'hui                                : jour de repos")
    if st.get_meta("needs_attention"):
        print(f"Intervention requise                       : {st.get_meta('needs_attention')}")
    table = st.seed_table(cfg["seed_score_days"])
    if table:
        k = cfg["seed_score_days"]
        scores = seed_scores(st, cfg, list(table))
        print(f"\nRendement des sources (follow-back sous {k} j ; estime = taux corrige du peu de donnees) :")
        for s in sorted(table, key=lambda s: -scores[s][0] / sum(scores[s])):
            v, (a, b) = table[s], scores[s]
            raw = f"{v['fb'] / v['n'] * 100:5.1f} %" if v["n"] else "    - "
            flags = "" if s in cfg["seeds"] else "  (retiree de la liste)"
            print(f"  @{s:<30} {v['total']:>5} suivis  {v['fb']:>4} / {v['n']:<5} {raw}"
                  f"   estime {a / (a + b) * 100:4.1f} %{flags}")
    rows = st.scored(cfg["seed_score_days"])
    for title, key in (("type de pseudo", handle_kind), ("moment de l'abonnement", day_part)):
        seg = segment_table(rows, key)
        if len(seg) < 2:
            continue
        scores = beta_scores(seg, list(seg), cfg["seed_prior_strength"])
        print(f"\nFollow-back par {title} (sous {cfg['seed_score_days']} j) :")
        for g in sorted(seg, key=lambda g: -scores[g][0] / sum(scores[g])):
            v, (a, b) = seg[g], scores[g]
            print(f"  {g:<31} {v['fb']:>4} / {v['n']:<5} {v['fb'] / v['n'] * 100:5.1f} %"
                  f"   estime {a / (a + b) * 100:4.1f} %")
        if key is handle_kind:
            n = min(seg.get(k, {"n": 0})["n"] for k in ("generique", "perso"))
            print("  filtre des pseudos generiques : " + (
                "desactive" if not cfg["handle_filter"] else "actif (ecartes si nettement moins rentables)"
                if n >= cfg["handle_filter_min"] else f"en attente ({n} / {cfg['handle_filter_min']} mesures)"))
    return 0


def cmd_check_seeds(cfg, here):
    """Visite chaque source une fois et ecarte celles qui n'existent pas (elles ne seront plus utilisees)."""
    st = State(DB_PATH)
    if not PROFILE.exists():
        log.error("Aucun profil navigateur : lance d'abord 'tiktok_rotate.py login'")
        return 2
    ok, ko = [], []
    with sync_playwright() as p:
        ctx = launch(p, cfg, hidden=not here)
        try:
            if not here:
                place_on_bot_desktop(cfg["virtual_desktop"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)
            if not logged_in(page):
                log.error("Session TikTok absente ou expiree : relance 'tiktok_rotate.py login'")
                return 3
            for seed in cfg["seeds"]:
                open_profile(page, seed)
                page.wait_for_timeout(rnd(2000, 5000))
                if blocked(page, cfg):
                    log.warning("Reaction de TikTok pendant la verification : arret")
                    break
                missing = visible(page.get_by_text(NOT_FOUND_RE))
                (ko if missing else ok).append(seed)
                st.set_meta(f"seed_fail:{seed}", "3" if missing else "0")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    log.info("Sources verifiees : %d valides, %d introuvables%s", len(ok), len(ko),
             " (" + ", ".join(ko) + ")" if ko else "")
    return 0


def cmd_diagnose(cfg):
    """Teste chaque selecteur sur ton profil et sur la premiere source, sans rien suivre ni desuivre.
    Captures et HTML des listes dans data/debug : de quoi mettre a jour la section UI (skill maj-selecteurs)."""
    if not PROFILE.exists():
        log.error("Aucun profil navigateur : lance d'abord 'tiktok_rotate.py login'")
        return 2
    cfg["captcha_wait_minutes"] = cfg["captcha_wait_minutes"] or 5

    def rep(label, value):
        log.info("  %-44s %s", label, value)

    js_labels = "els => [...new Set(els.map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())" \
                ".filter(Boolean))].slice(0, 20)"
    js_e2e = "els => [...new Set(els.map(e => e.getAttribute('data-e2e')))].slice(0, 80)"
    with sync_playwright() as p:
        ctx = launch(p, cfg, hidden=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)
            log.info("Diagnostic de l'interface TikTok")
            ok = logged_in(page)
            rep("connecte (LOGGED_IN_SEL)", ok)
            rep("bouton de connexion visible (LOGIN_SEL)", visible(page.locator(LOGIN_SEL)))
            me = detect_username(page)
            rep("pseudo detecte (NAV_PROFILE_SEL)", me or "-")
            if not ok:
                shot(page, cfg, "diag-home", force=True)
                return 3
            me = cfg["username"] or me
            targets = [("ton profil", me)] if me else []
            targets += [("source", s) for s in cfg["seeds"][:1]]
            for role, user in targets:
                log.info("@%s (%s)", user, role)
                open_profile(page, user)
                rep("reaction de TikTok", block_kind(page) or "aucune")
                rep("titre (USER_TITLE_SEL)", page.locator(USER_TITLE_SEL).count())
                rep("compteur d'abonnes (FOLLOWERS_COUNT_SEL)", page.locator(FOLLOWERS_COUNT_SEL).count())
                if role == "source":
                    state = profile_state(page)
                    btn = page.locator(PROFILE_BTN_SEL)
                    rep("bouton du profil (JS_PROFILE_BTN)", f"{state} ({btn.first.inner_text() if btn.count() else '-'})")
                rep("data-e2e de la page", page.locator("[data-e2e]").evaluate_all(js_e2e))
                shot(page, cfg, f"diag-profile-{user}", force=True)
                dlg, res = open_followers(page, user, cfg, load=False)
                rep("liste d'abonnes (DIALOG_SEL)", res)
                if dlg is None:
                    rep("fenetres role=dialog presentes", page.locator('[role="dialog"]').count())
                    shot(page, cfg, f"diag-list-{user}", force=True)
                    continue
                rows = list_users(dlg)
                rep("comptes dans la liste (ROW_LINK_SEL)", f"{len(rows)} : {sorted(rows)[:5]}")
                rep("textes des boutons de la liste", dlg.locator("button").evaluate_all(js_labels))
                for name, rx in (("Suivre", FOLLOW_RE), ("Suivre en retour", FOLLOW_BACK_RE),
                                 ("Abonne / Amis", FOLLOWING_RE)):
                    users = dlg.get_by_role("button", name=rx).evaluate_all(JS_USERS)
                    rep(f"boutons {name} -> pseudo de la ligne", f"{len(users)} : {users[:5]}")
                scroll_dialog(page, dlg)
                rep("defilement de la liste", f"{len(rows)} -> {len(rows | list_users(dlg))} comptes")
                SHOTS.mkdir(parents=True, exist_ok=True)
                html = SHOTS / f"{datetime.now():%Y%m%d-%H%M%S}-diag-list-{user}.html"
                html.write_text(dlg.evaluate("e => e.outerHTML"), encoding="utf-8")
                prune(SHOTS, "*.html", 10)
                shot(page, cfg, f"diag-list-{user}", force=True)
            log.info("Captures et HTML : %s", SHOTS)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def cmd_run(cfg, here, force=False):
    st = State(DB_PATH)
    attention = st.get_meta("needs_attention")
    if attention:
        log.error("Intervention requise (%s) : regle le probleme a la main dans TikTok, "
                  "puis lance 'tiktok_rotate.py login'", attention)
        return 5
    until = st.get_meta("blocked_until")
    if until and now() < datetime.fromisoformat(until):
        log.info("Pause anti-blocage jusqu'au %s : rien a faire", until)
        return 0
    if not PROFILE.exists():
        log.error("Aucun profil navigateur : lance d'abord 'tiktok_rotate.py login'")
        return 2
    if not force:
        if not in_active_hours(cfg, now()):
            log.info("Hors de la plage horaire %dh-%dh : rien a faire (--force pour passer outre)", *cfg["active_hours"])
            return 0
        if is_rest_day(cfg, today()):
            log.info("Jour de repos : rien a faire")
            return 0
    base = cfg["unfollow_after_days"]
    keep, keep_days = cfg["keep_followbacks"], cfg["followback_keep_days"]
    days = int(st.get_meta("current_days") or base) if cfg["adaptive_delay"] and cfg["username"] else base
    budget = follow_budget(st, cfg)
    quota = max(0, budget - st.followed_today())
    if st.following(keep) >= cfg["max_following"]:
        log.warning("%d comptes suivis par le script : pas de nouvel abonnement tant que les desabonnements "
                    "n'ont pas rattrape leur retard", cfg["max_following"])
        quota = 0
    room = max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today())
    if not quota and not (room and st.due(days, 1, keep, keep_days)):
        log.info("Rien a faire aujourd'hui (abonnements autorises : %d)", budget)
        return 0
    alive = [seed for seed in cfg["seeds"] if int(st.get_meta(f"seed_fail:{seed}") or 0) < 3]
    if quota and not alive:
        log.error("Aucune source valide : remplis seeds.txt ou lance 'tiktok_rotate.py check-seeds'")
        return 1
    left = minutes_left(st, cfg)
    if left is not None and left < 5:
        log.info("Temps de passage du jour deja utilise (%d min) : rien a faire", cfg["daily_minutes"])
        return 0
    cfg["_deadline"] = now() + timedelta(minutes=left) if left is not None else None
    cfg["_block"], cfg["_recent"], cfg["_checked"] = None, [], set()
    started = now()
    longest = max(cfg["unfollow_days_max"], base)
    counter = [0]
    unfollows = follows = 0
    status = "ok"
    if cfg["keep_awake"]:
        keep_awake(True)
    try:
        with sync_playwright() as p:
            ctx = launch(p, cfg, hidden=not here)
            try:
                if not here:
                    place_on_bot_desktop(cfg["virtual_desktop"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(15000)
                if not logged_in(page):
                    log.error("Session TikTok absente ou expiree : relance 'tiktok_rotate.py login'")
                    return 3
                remember_username(page, cfg)
                if blocked(page, cfg):  # captcha (non resolu) ou message des l'accueil
                    status = "blocked"
                analysis = bool(cfg["adaptive_delay"] and cfg["username"])
                if cfg["adaptive_delay"] and not cfg["username"]:
                    log.warning("Pseudo inconnu : pas de mesure des follow-back, delai fixe de %d jours", base)

                def fixed_days():  # groupe temoin : une part des comptes reste suivie le delai le plus long
                    return longest if analysis and random.random() < cfg["explore_share"] else None

                if analysis:
                    days = int(st.get_meta("current_days") or base)
                    if status == "ok" and st.get_meta("last_scan_on") != today().isoformat():  # une lecture par jour
                        scan = scan_my_followers(page, cfg["username"], st, cfg)[0]
                        if scan == "ok":
                            st.set_meta("last_scan_on", today().isoformat())
                        elif scan == "blocked":
                            status = "blocked"
                    stats = followback_stats(st.samples(cfg["adapt_window_days"]), cfg, base)
                    log.info("Follow-back : %s", describe(stats))
                    if stats["delay"] != days:
                        log.info("Delai de desabonnement ajuste : %d -> %d jours", days, stats["delay"])
                    days = stats["delay"]
                    st.set_meta("current_days", str(days))

                def unfollows_due():
                    room = max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today())
                    return bool(room and st.due(days, 1, keep, keep_days))

                if status == "ok" and unfollows_due():
                    unfollows, status = unfollow_pass(page, st, cfg, counter, days, unfollow_until(cfg, quota))
                    # part du temps ecoulee, ou plafond horaire des desabonnements : les abonnements ont le leur
                    if status in ("later", "stop") and not day_over(cfg):
                        status = "ok"
                if status == "ok" and quota:
                    follows, status = follow_pass(page, st, cfg, counter, alive, quota, fixed_days)
                    if status == "stop" and not day_over(cfg):
                        status = "ok"
                if status == "ok" and unfollows_due():  # temps restant apres les abonnements
                    n, status = unfollow_pass(page, st, cfg, counter, days)
                    unfollows += n
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception:
        log.exception("Erreur inattendue")
        return 1
    finally:
        add_run_time(st, started)
        keep_awake(False)

    kind = cfg.get("_block") or "limit"
    if status == "blocked" and kind == "attention":
        st.set_meta("needs_attention", f"{now():%Y-%m-%d %H:%M} bannissement, suspension ou deconnexion")
        log.error("TikTok demande une intervention (compte banni ou suspendu, ou deconnexion) : "
                  "arret total. Regle-le a la main, puis lance 'tiktok_rotate.py login'")
    elif status == "blocked":
        hours, factor = register_block(st, cfg, kind)
        log.warning("Reaction de TikTok (%s) : pause de %d h, quotas ramenes a %d %%", kind, hours, factor * 100)
    else:
        register_clean_day(st)
    log.info("Termine : %d abonnement(s), %d desabonnement(s)", follows, unfollows)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["login", "run", "status", "check-seeds", "diagnose"])
    ap.add_argument("--here", action="store_true",
                    help="run / check-seeds : garder la fenetre visible sur le bureau courant (debug)")
    ap.add_argument("--force", action="store_true",
                    help="run : ignorer la plage horaire et le jour de repos (debug)")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "status":
        return cmd_status(cfg)
    if not acquire_lock():
        log.error("Une autre instance est deja en cours")
        return 4
    if args.cmd == "login":
        return cmd_login(cfg)
    if args.cmd == "check-seeds":
        return cmd_check_seeds(cfg, args.here)
    if args.cmd == "diagnose":
        return cmd_diagnose(cfg)
    return cmd_run(cfg, args.here, args.force)


if __name__ == "__main__":
    sys.exit(main())
