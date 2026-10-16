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
FRIENDS_RE = re.compile(r"^(Ami\(e\)s|Amis|Friends)", re.I)  # abonnement mutuel : il te suit aussi
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
# Requete envoyee par la page a chaque (des)abonnement. Sa reponse JSON dit si TikTok a vraiment pris
# l'action en compte : status_code 0 = accepte, sinon refus (status_msg = raison). Lecture seule.
FOLLOW_API_RE = re.compile(r"/api/commit/follow/user/")
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
# Textes ecrits par l'utilisateur d'un profil : ils ne sont jamais une reaction de TikTok (une bio "trop
# rapidement" ou "Try again later" ne doit pas valoir une pause).
PROFILE_TEXT_SEL = '[data-e2e="user-bio"], [data-e2e="user-subtitle"], [data-e2e="user-title"]'
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

# Parmi des elements dont le texte ressemble a une reaction de TikTok, nombre de ceux qui ne sont pas ecrits
# par un utilisateur : hors PROFILE_TEXT_SEL, et hors ligne de liste ou carte de compte (premier ancetre qui
# contient des liens de profil, ton lien de menu exclu : un seul compte = sa ligne). Un message de TikTok
# (toast, fenetre) n'est dans aucune ligne : on remonte jusqu'a la page ou la fenetre.
JS_OUTSIDE_PROFILES = r"""(els, args) => els.filter(el => {
  if (el.closest(args.skip)) return false;
  const user = a => { const m = (a.getAttribute('href') || '').match(/\/@([\w.]+)/); return m ? m[1].toLowerCase() : null; };
  for (let n = el, i = 0; i < 8 && n; i++, n = n.parentElement) {
    // page ou fenetre entiere (le lien vers ton profil n'est dans aucune ligne) : message de TikTok
    if (n === document.body || n.matches(args.dialog) || n.querySelector(args.nav)) return true;
    const links = [...n.querySelectorAll('a[href*="/@"]')].filter(a => !a.matches(args.nav));
    const k = new Set(links.map(user).filter(Boolean)).size;  // /@u et /@u/video/1 : un seul compte
    if (k === 1) return false;
    if (k > 1) return true;
  }
  return true;
}).length"""

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
    "follows_per_seed": 10,
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
    # seuls les jours d'abonnement de cette fenetre comptent : apres une longue absence, montee en charge
    # reprise depuis ramp_start (repartir d'un coup a plein regime est un signal classique)
    "ramp_window_days": 14,
    "max_follows_per_hour": 20,
    "max_unfollows_per_hour": 25,
    "max_unfollows_per_day": 100,
    "max_following": 1000,
    # part du temps du jour au plus pour les desabonnements tant qu'il reste des abonnements a faire
    "unfollow_time_share": 0.5,
    # sessions : la tache planifiee reveille le script toutes les 20 min (run --auto) ; il fait une session
    # par partie de la journee (matin, apres-midi, soir), debut tire au hasard dans les
    # session_jitter_minutes, abonnements places dans les parties au meilleur follow-back mesure.
    # Le temps du jour (daily_minutes) est reparti selon les parties ou le PC est allume d'habitude
    # (attendance_days derniers jours). false : une seule session par jour.
    "day_sessions": True,
    "session_jitter_minutes": 45,
    "session_min_minutes": 15,
    "attendance_days": 14,
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
            " source TEXT, fb_late INTEGER);"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        if "fb_late" not in cols:  # base anterieure
            self.db.execute("ALTER TABLE users ADD COLUMN fb_late INTEGER")
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

    def followed_back(self, user):
        return self.count("username=? AND followed_back_on IS NOT NULL", (user,)) > 0

    def mark_followed_back(self, user, late=False):
        """late : follow-back vu seulement au desabonnement (la lecture de tes abonnes l'a manque) ; sa date
        n'est qu'une borne, il ne compte pas comme un delai mesure (voir samples)."""
        self.db.execute("UPDATE users SET followed_back_on=?, fb_late=? WHERE username=? AND followed_back_on IS NULL",
                        (today().isoformat(), 1 if late else None, user))
        self.db.commit()

    def followed_today(self):
        return self.count("followed_on=?", (today().isoformat(),))

    def unfollowed_today(self):
        return self.count("unfollowed_on=? AND unfollowed_at IS NOT NULL", (today().isoformat(),))

    def following(self, keep_followbacks=False):
        """Abonnements en cours faits par le script (hors follow-back gardes si keep_followbacks)."""
        return self.count("unfollowed_on IS NULL" + (" AND followed_back_on IS NULL" if keep_followbacks else ""))

    def active_follow_days(self, window_days):
        """Jours des `window_days` derniers (avant aujourd'hui) ou des abonnements ont ete faits : sert a la
        montee en charge."""
        return self.db.execute(
            "SELECT COUNT(DISTINCT followed_on) FROM users WHERE followed_on < ? AND followed_on >= ? AND "
            + REAL_FOLLOW,
            (today().isoformat(), (today() - timedelta(days=window_days)).isoformat())).fetchone()[0]

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
        for f, fb, uf, late in self.db.execute(
                "SELECT followed_on, followed_back_on, unfollowed_on, fb_late FROM users WHERE followed_on>=? AND "
                + REAL_FOLLOW, (since,)):
            start = date.fromisoformat(f)
            if fb and not late:
                out.append(((date.fromisoformat(fb) - start).days, True))
            elif fb:  # date inconnue (vu au desabonnement) : comme avant, observe sans follow-back jusque-la
                out.append(((date.fromisoformat(fb) - start).days, False))
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

    def del_meta(self, key):
        self.db.execute("DELETE FROM meta WHERE key=?", (key,))
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


# Parties de la journee : le follow-back est mesure par partie, et il y a une session par partie.
DAY_PARTS = (("matin (avant 12 h)", 0, 12), ("apres-midi (12-18 h)", 12, 18), ("soir (apres 18 h)", 18, 24))


def part_of_hour(hour):
    return next(name for name, lo, hi in DAY_PARTS if lo <= hour < hi)


def day_part(user, at):
    return part_of_hour(int(at[11:13]))


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
    ramp = cfg["ramp_start"] + cfg["ramp_step"] * st.active_follow_days(cfg["ramp_window_days"])
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
    """Passage sans reaction de TikTok : le jour est candidat. Il ne compte qu'une fois fini (settle_clean_day),
    sans blocage entre-temps : avec plusieurs sessions par jour, la premiere ne doit pas relever les quotas
    des suivantes."""
    if st.get_meta("last_clean_on") != today().isoformat():
        st.set_meta("clean_candidate", today().isoformat())


def settle_clean_day(st):
    """Un jour fini sans reaction de TikTok : les quotas remontent de 10 points, jusqu'a 100 %. Un blocage ce
    jour-la (register_block met last_clean_on a la date du blocage) annule le credit."""
    day = st.get_meta("clean_candidate")
    if not day or day >= today().isoformat():
        return
    st.del_meta("clean_candidate")
    if day == st.get_meta("last_clean_on"):
        return
    st.set_meta("last_clean_on", day)
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


def add_run_time(st, cfg):
    """Ajoute le temps ecoule depuis le dernier releve au temps du jour et a celui de la partie de la session.
    Releve avant chaque action (throttle) et en fin de passage : un PC eteint en pleine session ne fait pas
    perdre le decompte."""
    moment = now()
    # entre deux releves, au plus l'attente la plus longue prevue (plafond horaire) : au-dela, le PC etait en
    # veille (couvercle ferme), ce temps ne compte pas
    secs = min((moment - cfg["_tick"]).total_seconds(), (cfg["max_wait_minutes"] + 15) * 60)
    cfg["_tick"] = moment
    day = today().isoformat()
    for key in (f"run_seconds:{day}", f"run_seconds:{day}:{cfg['_part']}"):
        st.set_meta(key, str(float(st.get_meta(key) or 0) + secs))


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
    desabonnements (apres une pause) prendrait tout le temps du jour et aucun abonnement ne se ferait.
    En session, la part vient du plan de la session (_unfollow_share)."""
    deadline, share = cfg.get("_deadline"), cfg.get("_unfollow_share", cfg["unfollow_time_share"])
    if not (quota and deadline) or share >= 1:
        return None
    return now() + max(timedelta(0), deadline - now()) * share


# ---------------------------------------------------------------------------
# Sessions de la journee : quand agir, combien de temps, et pour quoi faire
# ---------------------------------------------------------------------------
def part_windows(cfg, day):
    """[(partie, ouverture, debut de la session, fin)] du jour, dans la plage horaire. Le debut est tire au
    hasard dans les session_jitter_minutes apres l'ouverture, une fois par date (stable d'un reveil a
    l'autre). day_sessions false : une seule partie, toute la plage horaire."""
    lo_h, hi_h = cfg["active_hours"]
    parts = DAY_PARTS if cfg["day_sessions"] else (("journee", 0, 24),)
    midnight = datetime.combine(day, datetime.min.time())
    out = []
    for name, lo, hi in parts:
        a, b = max(lo, lo_h), min(hi, hi_h)
        if a >= b:
            continue
        jitter = min(cfg["session_jitter_minutes"], max(0, (b - a) * 60 - 60))  # au moins 1 h de session
        offset = random.Random(f"session-{day.isoformat()}-{name}").uniform(0, jitter)
        opens = midnight + timedelta(hours=a)
        out.append((name, opens, opens + timedelta(minutes=offset), midnight + timedelta(hours=b)))
    return out


def current_part(windows, moment):
    return next((w for w in windows if w[1] <= moment < w[3]), None)


def mark_seen(st, part):
    """Note que le PC etait allume pendant la session de `part` aujourd'hui (60 jours gardes)."""
    seen = json.loads(st.get_meta("parts_seen") or "{}")
    day = today().isoformat()
    if part in seen.get(day, []):
        return
    seen.setdefault(day, []).append(part)
    cutoff = (today() - timedelta(days=60)).isoformat()
    st.set_meta("parts_seen", json.dumps({d: v for d, v in seen.items() if d >= cutoff}))


def attendance(seen, day, cur, names, window_days):
    """{partie: probabilite que sa session ait lieu}, d'apres les jours passes de la fenetre (aujourd'hui
    exclu) ou la session `cur` a eu lieu. A priori 1/2 : sans historique, une partie suivante compte pour
    une demi-session."""
    since = (day - timedelta(days=window_days)).isoformat()
    days = [d for d, parts in seen.items() if cur in parts and since <= d < day.isoformat()]
    return {q: (sum(q in seen[d] for d in days) + 1) / (len(days) + 2) for q in names}


def part_draw(st, cfg, names):
    """{partie: taux de follow-back tire}, une fois par jour (meme classement pour toutes les sessions du
    jour) : echantillonnage de Thompson sur le follow-back mesure par partie, comme pour les sources. La
    meilleure partie recoit le plus souvent les abonnements, les autres gardent leur chance d'etre mesurees."""
    saved = json.loads(st.get_meta("part_draw") or "{}")
    if saved.get("day") == today().isoformat() and set(names) <= set(saved["draw"]):
        return saved["draw"]
    table = segment_table(st.scored(cfg["seed_score_days"]), day_part)
    draw = {p: random.betavariate(a, b) for p, (a, b) in beta_scores(table, names, cfg["seed_prior_strength"]).items()}
    st.set_meta("part_draw", json.dumps({"day": today().isoformat(), "draw": draw}))
    return draw


def action_seconds(st, cfg, kind):
    """Duree moyenne d'une action ('follow' ou 'unfollow') pauses comprises : mesuree (moyenne glissante),
    sinon estimee d'apres le rythme."""
    saved = st.get_meta(f"sec_per_{kind}")
    if saved:
        return float(saved)
    lo, hi = cfg["pause_jitter"]
    llo, lhi = cfg["long_pause_seconds"]
    return 3600 / cfg[f"max_{kind}s_per_hour"] * (lo + hi) / 2 + (llo + lhi) / 2 / cfg["long_pause_every"] + 20


def learn_seconds(st, cfg, kind, seconds, n):
    if n >= 5:
        st.set_meta(f"sec_per_{kind}", f"{0.7 * action_seconds(st, cfg, kind) + 0.3 * seconds / n:.1f}")


def plan_session(cur, window, later, R, used, Q, done, U, theta, spf, share_u):
    """Temps et abonnements de la session de la partie `cur`. Retourne (minutes, abonnements au plus, part du
    temps de la session pour les desabonnements avant les abonnements).

    window : minutes d'ici la fin de la partie ; later : [(partie, minutes de sa session, probabilite qu'elle
    ait lieu)] pour les parties suivantes ; R : minutes de passage restantes aujourd'hui ; used, done :
    minutes et abonnements deja faits dans cette partie ; Q : abonnements restants aujourd'hui ; U : minutes
    de desabonnements en attente ; theta : {partie: taux de follow-back tire} ; spf : secondes par
    abonnement ; share_u : part du temps au plus pour les desabonnements.

    Le temps T de cette partie et des suivantes est partage : chaque partie suivante en garde ce qu'elle
    utiliserait si elle a lieu (probabilite x part egale), celle-ci prend le reste, et la derniere tout ce
    qui reste (un PC eteint plus tard ne fait pas perdre de temps). Les abonnements qui tiennent dans T
    (desabonnements deduits, a hauteur de share_u) vont d'abord aux parties au meilleur taux tire ; les
    desabonnements remplissent le reste du temps de chaque session."""
    T = R + used
    if later:
        s = T / (1 + sum(a for _, _, a in later))
        caps = {q: a * min(w, s) for q, w, a in later}
        cap_c = max(0.0, min(window + used, T - sum(caps.values())))
    else:
        caps, cap_c = {}, min(window + used, T)
    minutes = max(0.0, cap_c - used)
    k = 60 / spf  # abonnements par minute
    left = min(Q + done, max(0.0, T - min(U, share_u * T)) * k)
    caps[cur] = cap_c
    alloc = {}
    for p in sorted(caps, key=lambda p: -theta.get(p, 0.0)):
        alloc[p] = min(left, caps[p] * k)
        left -= alloc[p]
    f_now = max(0.0, alloc[cur] - done)
    quota = Q if not later else min(Q, int(round(f_now)))
    ushare = 1.0 if minutes <= 0 else 1 - min(1.0, f_now / k / minutes)
    return minutes, quota, ushare


def session_plan(st, cfg, windows, cur, moment, quota, n_due):
    """Rassemble les mesures (temps restant, presence du PC, follow-back par partie, duree des actions) et
    appelle plan_session."""
    name, opens, start, end = cur
    later = [w for w in windows if w[1] >= end]
    att = attendance(json.loads(st.get_meta("parts_seen") or "{}"), today(), name, [w[0] for w in later],
                     cfg["attendance_days"])
    spans = [(w[0], (w[3] - w[2]).total_seconds() / 60, att[w[0]]) for w in later]
    window = (end - moment).total_seconds() / 60
    R = minutes_left(st, cfg)
    if R is None:  # temps illimite : celui des sessions
        R = window + sum(m for _, m, _ in spans)
    used = float(st.get_meta(f"run_seconds:{today().isoformat()}:{name}") or 0) / 60
    done = st.count("followed_at >= ? AND followed_at < ?",
                    (opens.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))
    theta = part_draw(st, cfg, [w[0] for w in windows])
    return plan_session(name, window, spans, R, used, quota, done,
                        n_due * action_seconds(st, cfg, "unfollow") / 60, theta,
                        action_seconds(st, cfg, "follow"), cfg["unfollow_time_share"])


def throttle(page, st, cfg, kind):
    """Respecte le plafond horaire (cumule sur tous les passages), la plage horaire et le temps de
    passage du jour. Retourne False s'il faut s'arreter pour aujourd'hui."""
    limit = cfg[f"max_{kind}s_per_hour"]
    deadline = cfg.get("_deadline")
    if "_tick" in cfg:
        add_run_time(st, cfg)
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
            if pid != os.getpid() and "tiktok_rotate" in " ".join(psutil.Process(pid).cmdline()):
                return False
        except (ValueError, psutil.Error):  # verrou illisible, processus fini ou autre programme : on le reprend
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


def tiktok_says(page, rx):
    """True si un texte visible correspond a `rx` hors des textes ecrits par des utilisateurs (voir
    JS_OUTSIDE_PROFILES)."""
    try:
        return page.get_by_text(rx).filter(visible=True).evaluate_all(
            JS_OUTSIDE_PROFILES, {"skip": PROFILE_TEXT_SEL, "dialog": DIALOG_SEL, "nav": NAV_PROFILE_SEL}) > 0
    except Exception:
        return False


def account_missing(page):
    """Page "compte introuvable" : le message, sans profil affiche (le meme texte dans une bio ne compte pas)."""
    return visible(page.get_by_text(NOT_FOUND_RE)) and not visible(page.locator(USER_TITLE_SEL))


def captcha_visible(page):
    return visible(page.locator(CAPTCHA_SEL)) or tiktok_says(page, CAPTCHA_RE)


def block_kind(page):
    """None, 'limit' (rythme trop rapide), 'captcha' (verification anti-robot), 'warning' (automatisation
    soupconnee) ou 'attention' (compte banni ou suspendu, deconnexion)."""
    try:
        if ATTENTION_URL_RE.search(page.url) or visible(page.locator(LOGIN_SEL)):
            return "attention"
        if tiktok_says(page, ATTENTION_RE):
            return "attention"
        if captcha_visible(page):
            return "captcha"
        for kind, rx in (("warning", WARN_RE), ("limit", BLOCK_RE)):
            if tiktok_says(page, rx):
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
            # signe de soupcon de TikTok : on ne repart pas au meme rythme
            cfg["_slow"] = min(3.0, cfg.get("_slow", 1.0) * 1.5)
            log.info("Captcha resolu : reprise, pauses x %.1f jusqu'a la fin du passage", cfg["_slow"])
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


def watch_api(page, cfg):
    """Garde les reponses de TikTok aux (des)abonnements de la page (lecture seule, rien n'est modifie)."""
    cfg["_api"], cfg["_api_misses"] = [], 0
    page.on("response", lambda r: cfg["_api"].append(r) if FOLLOW_API_RE.search(r.url) else None)


def api_clear(cfg):
    if "_api" in cfg:
        cfg["_api"] = []


def api_reply(page, cfg):
    """(status_code, status_msg) de la reponse de TikTok a la derniere action, ou None si aucune reponse
    lisible : on se fie alors au bouton seul. Attente de 3 s au plus, sauf si l'API semble introuvable."""
    if "_api" not in cfg:
        return None
    if cfg["_api_misses"] < 3:
        for _ in range(12):  # 3 s au plus
            if cfg["_api"]:
                break
            page.wait_for_timeout(250)
    responses, cfg["_api"] = cfg["_api"], []
    for r in reversed(responses):
        try:
            data = r.json()
        except Exception:
            continue
        if isinstance(data, dict) and "status_code" in data:
            cfg["_api_misses"] = 0
            return int(data["status_code"] or 0), str(data.get("status_msg") or "")
    return None


def api_missing(cfg):
    """Action confirmee par le bouton sans reponse de l'API vue (un clic ignore n'envoie rien, il ne compte
    pas) : apres 3 de suite, l'adresse a sans doute change et on n'attend plus la reponse."""
    if "_api" not in cfg:
        return
    cfg["_api_misses"] += 1
    if cfg["_api_misses"] == 3:
        log.warning("Reponse de TikTok aux abonnements introuvable (FOLLOW_API_RE) : controle par le bouton "
                    "seulement ; voir la skill maj-selecteurs")


def api_reaction(reply):
    """None si TikTok a accepte l'action (ou si sa reponse n'a pas ete vue) ; sinon 'warning' ou 'limit' quand
    le message est reconnu, 'refused' pour un refus sans message connu."""
    if not reply or reply[0] == 0:
        return None
    if WARN_RE.search(reply[1]):
        return "warning"
    if BLOCK_RE.search(reply[1]):
        return "limit"
    return "refused"


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
    secs = 3600 / cfg[f"max_{kind}s_per_hour"] * rnd(*cfg["pause_jitter"]) * cfg.get("_slow", 1.0)
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
    """Pas abonne : 'follow', ou 'fan' s'il te suit ("Suivre en retour"). Abonne : 'friends' s'il te suit
    aussi ("Amis"), sinon 'following' (abonne ou demande envoyee). None : bouton introuvable.
    Le bouton trouve est marque : page.locator(PROFILE_BTN_SEL)."""
    pattern = f"(?:{NOT_FOLLOWING_RE.pattern})|(?:{FOLLOWING_RE.pattern})"
    text = page.evaluate(JS_PROFILE_BTN, {"title": USER_TITLE_SEL, "re": pattern})
    if text is None:
        return None
    if NOT_FOLLOWING_RE.match(text):
        return "fan" if FOLLOW_BACK_RE.match(text) else "follow"
    return "friends" if FRIENDS_RE.match(text) else "following"


def open_followers(page, user, cfg, load=True):
    """Ouvre la liste d'abonnes de `user`. Retourne (fenetre, 'ok' | 'blocked' | 'missing' | 'hidden')."""
    if load:
        open_profile(page, user)
    if blocked(page, cfg):
        return None, "blocked"
    if account_missing(page):
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
    attempts = cfg.setdefault("_attempts", [])
    # comptes ecartes pour tout le passage : une source peut etre rouverte plusieurs fois (tranches)
    skip, rows = cfg.setdefault("_skip", set()), set()
    done = scrolls = stalls = filtered = 0
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
            api_clear(cfg)
            try:
                btns.nth(idx).click(timeout=8000)
            except PWTimeout:
                skip.add(user)
                continue
            page.wait_for_timeout(rnd(1500, 2500))
            if blocked(page, cfg):
                shot(page, cfg, "blocked-follow")
                return done, "blocked"
            reply = api_reply(page, cfg)
            reaction = api_reaction(reply)
            if reaction in ("warning", "limit"):
                log.warning("[%s] TikTok refuse l'abonnement a @%s : %s", seed, user, reply[1])
                cfg["_block"] = reaction
                shot(page, cfg, "blocked-follow")
                return done, "blocked"
            if reaction == "refused" or not follow_registered(page, dlg, user):
                # refus de TikTok (meme si le bouton affiche Abonne), ou bouton reste ou revenu sur "Suivre" :
                # TikTok a ignore le clic (limitation silencieuse). Le clic est enregistre (il compte dans les
                # plafonds et le compte ne sera plus clique) mais exclu des statistiques (PHANTOM).
                attempts.append(True)
                st.add_follow(user, seed)
                st.mark_unfollowed(user, PHANTOM, action=False)
                log.info("[%s] abonnement a @%s non pris en compte%s (%d sur les %d derniers essais)", seed, user,
                         f" (refus de TikTok, code {reply[0]})" if reaction else "", sum(attempts[-12:]),
                         len(attempts[-12:]))
                shot(page, cfg, f"follow-{user}")
                if follow_failing(attempts):
                    cfg["_block"] = "limit"
                    return done, "blocked"
                human_pause(page, cfg, counter[0], "follow")  # le clic a eu lieu : pas de nouvel essai en rafale
                continue
            if reply is None:
                api_missing(cfg)
            attempts.append(False)
            st.add_follow(user, seed, fixed_days())
            done += 1
            counter[0] += 1
            recent.append(user)
            log.info("[%s] abonne a @%s (%d/%d)", seed, user, done, quota)
            human_pause(page, cfg, counter[0], "follow")
        return done, "ok"
    finally:
        if filtered:
            log.info("[%s] %d pseudo(s) generique(s) ecarte(s) (follow-back mesure trop faible)", seed, filtered)


def follow_failing(attempts):
    """attempts : essais d'abonnement du passage, dans l'ordre (True = ignore ou refuse). 3 echecs de suite, ou
    4 sur les 12 derniers (un echec sur deux ne passe plus inapercu), valent une limitation."""
    tail = 0
    for failed in reversed(attempts):
        if not failed:
            break
        tail += 1
    return tail >= 3 or sum(attempts[-12:]) >= 4


def follow_registered(page, dlg, user):
    """True quand le bouton de la ligne est passe a Abonne / Amis / Demande ; TikTok met parfois plusieurs
    secondes. False seulement s'il est encore sur Suivre apres ~10 s."""
    for _ in range(5):
        if user in dlg.get_by_role("button", name=FOLLOWING_RE).evaluate_all(JS_USERS):
            return True
        page.wait_for_timeout(2000)
    return user not in dlg.get_by_role("button", name=FOLLOW_RE).evaluate_all(JS_USERS)


def follow_sticks(page, user, cfg):
    """Ouvre le profil de `user` (entre deux tranches : la liste de la source est rouverte ensuite ; pas de
    second onglet, qui pourrait s'ouvrir sur ton bureau Windows). Retourne 'ok' si l'abonnement y figure,
    'lost' si le bouton est sur Suivre, 'blocked' si TikTok reagit, None si on ne peut pas conclure."""
    try:
        open_profile(page, user)
        if blocked(page, cfg):
            shot(page, cfg, "blocked-verify")
            return "blocked"
        if account_missing(page):
            return None
        state = profile_state(page)
        return None if state is None else ("ok" if state in ("following", "friends") else "lost")
    except PWTimeout:  # sondage facultatif : une page lente ne doit pas arreter le passage
        return None


def verify_recent(page, st, cfg, n):
    """Sondage contre la limitation silencieuse : le bouton de la liste peut afficher "Abonne" alors que
    TikTok n'a rien enregistre. Un des `n` abonnements de la tranche qui vient de finir (le dernier a eu sa
    pause, TikTok a eu le temps) est verifie sur le profil du compte ; s'il a disparu, un second est verifie.
    Deux disparus = TikTok ignore les abonnements : on s'arrete avant que la restriction ne s'aggrave.
    Retourne 'ok' ou 'blocked'."""
    checked = cfg.setdefault("_checked", set())
    pool = [u for u in cfg.get("_recent", [])[-n:] if u not in checked]
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


def cautious(st, cfg):
    """Apres une pause anti-blocage (quotas pas encore remontes) ou un captcha resolu dans le passage."""
    return cfg.get("_slow", 1.0) > 1 or float(st.get_meta("quota_factor") or 1.0) < 1


def chunk_size(st, cfg, remaining):
    """Abonnements de la prochaine tranche : follows_per_seed, au plus verify_every (un sondage par tranche),
    3 en periode de prudence (une limitation qui persiste est vue apres 3 abonnements, pas 10)."""
    size = cfg["follows_per_seed"] or remaining
    if cfg["verify_every"]:
        size = min(size, 3 if cautious(st, cfg) else cfg["verify_every"])
    return max(1, min(size, remaining))


def follow_pass(page, st, cfg, counter, seeds, quota, fixed_days):
    """Abonnements du passage, par tranches de follows_per_seed. Apres chaque tranche la source est tiree a
    nouveau : la meme peut revenir, et sa liste rouverte montre alors ses abonnes les plus recents, en ligne
    a cet instant (ils voient la notification tout de suite). Les tirages repartis sur plusieurs sources
    font aussi apprendre plus vite laquelle rend le mieux. Une source epuisee ou inaccessible est ecartee
    pour le passage, seeds_per_run fois au plus. Retourne (nombre, 'ok' | 'stop' | 'blocked')."""
    remaining, follows, misses, out = quota, 0, 0, set()
    wanted = handle_filter(st, cfg)
    while remaining > 0 and misses < cfg["seeds_per_run"]:
        size = chunk_size(st, cfg, remaining)
        pool = [s for s in seed_order(st, cfg, seeds) if s not in out]
        if not pool:
            break
        seed = pool[0]
        if cfg["seed_scoring"]:
            a, b = seed_scores(st, cfg, [seed])[seed]
            log.info("[%s] source choisie (follow-back estime %.1f %%)", seed, a / (a + b) * 100)
        n, s = follow_from_seed(page, seed, size, st, cfg, counter, fixed_days, wanted)
        follows += n
        remaining -= n
        if n and cfg["verify_every"] and s in ("ok", "dry") and verify_recent(page, st, cfg, n) == "blocked":
            s = "blocked"
        if s == "skip":  # source inaccessible : ecartee apres 3 echecs, un par jour au plus
            if st.get_meta(f"seed_fail_on:{seed}") != today().isoformat():
                st.set_meta(f"seed_fail_on:{seed}", today().isoformat())
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
def unfollow_one(page, user, st, cfg, spare_friends=False):
    """Retourne 'ok' | 'gone' | 'already' | 'friend' | 'refused' | 'failed' | 'blocked'.
    'already' = deja plus abonne ; 'friend' = il te suit (vu sur son profil, note comme follow-back) et
    spare_friends : rien n'est clique. 'refused' = TikTok a refuse le desabonnement."""
    open_profile(page, user)
    if blocked(page, cfg):
        return "blocked"
    if account_missing(page):
        return "gone"
    state = profile_state(page)
    if state in ("fan", "friends"):  # follow-back que la lecture de tes abonnes n'a pas vu
        st.mark_followed_back(user, late=True)
    if state in ("follow", "fan"):
        return "already"
    if state == "friends" and spare_friends:
        return "friend"
    if state is None:
        shot(page, cfg, f"unfollow-{user}-nobutton")
        return "failed"
    api_clear(cfg)
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
    reply = api_reply(page, cfg)
    reaction = api_reaction(reply)
    if reaction in ("warning", "limit"):
        log.warning("TikTok refuse le desabonnement de @%s : %s", user, reply[1])
        cfg["_block"] = reaction
        return "blocked"
    if reaction == "refused":
        log.info("TikTok refuse le desabonnement de @%s (code %d)", user, reply[0])
        shot(page, cfg, f"unfollow-{user}-refused")
        return "refused"
    for _ in range(4):
        if profile_state(page) in ("follow", "fan"):
            if reply is None:
                api_missing(cfg)
            return "ok"
        page.wait_for_timeout(1500)
    shot(page, cfg, f"unfollow-{user}")
    return "failed"


def unfollow_trouble(st, user):
    """(jours d'ennui, date du dernier) d'un desabonnement en echec ou refuse."""
    raw = st.get_meta(f"unfollow_trouble:{user}") or ""
    day, _, n = raw.rpartition(":")
    return (int(n) if n.isdigit() else 0), day


def bump_trouble(st, user):
    """Un ennui de plus pour `user`, au plus un par jour (les reveils frequents ne doivent pas l'abandonner en
    quelques heures). Retourne (jours d'ennui, premier ennui ?)."""
    n, day = unfollow_trouble(st, user)
    if day != today().isoformat():
        st.set_meta(f"unfollow_trouble:{user}", f"{today().isoformat()}:{n + 1}")
        return n + 1, n == 0
    return n, False


def unfollow_pass(page, st, cfg, counter, days, until=None):
    """Desabonne les comptes arrives a echeance ; `until` : fin de la part du temps qui leur est reservee.

    Echecs et refus : un compte deja en difficulte passe en dernier, et son compteur monte au plus une fois
    par jour. Si les desabonnements marchent (un a reussi dans ce passage, ou depuis hier), l'ennui d'un
    compte deja en difficulte lui est propre : il ne compte pas dans les series, et apres 3 jours de refus
    (5 d'echec) le compte est abandonne (il reste suivi). Sinon tout compte : 3 echecs = interface modifiee
    ('broken'), 3 refus = limitation. Un compte n'est essaye qu'une fois par passage.
    Retourne (nombre, 'ok' | 'later' | 'stop' | 'blocked' | 'broken')."""
    tried = cfg.setdefault("_tried", set())
    due = [u for u in st.due(days, max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today()),
                             cfg["keep_followbacks"], cfg["followback_keep_days"]) if u not in tried]
    due.sort(key=lambda u: unfollow_trouble(st, u)[0] > 0)
    log.info("%d compte(s) a desabonner", len(due))
    done = fails = phantoms = refused = 0
    # les abonnements d'avant le dernier blocage l'ont deja provoque (bases anciennes : fin de la pause)
    since = st.get_meta("last_block_at") or st.get_meta("blocked_until") or ""
    spare = cfg["keep_followbacks"] or cfg["followback_keep_days"] > 0
    yesterday = (today() - timedelta(days=1)).isoformat()
    for user in due:
        if until and now() >= until:
            log.info("Part du temps reservee aux desabonnements utilisee : place aux abonnements")
            return done, "later"
        if not throttle(page, st, cfg, "unfollow"):
            return done, "stop"
        tried.add(user)
        # un follow-back deja connu n'arrive ici qu'une fois sa periode de grace passee
        res = unfollow_one(page, user, st, cfg, spare_friends=spare and not st.followed_back(user))
        if res == "blocked":
            return done, "blocked"
        if res == "friend":
            log.info("@%s te suit en retour (absent de la lecture de tes abonnes) : garde", user)
            continue
        if res == "ok":
            if unfollow_trouble(st, user)[0]:
                st.del_meta(f"unfollow_trouble:{user}")
            if not done:
                st.set_meta("last_unfollow_ok_on", today().isoformat())
            st.mark_unfollowed(user)
            done += 1
            fails = phantoms = refused = 0
            counter[0] += 1
            log.info("desabonne de @%s", user)
            human_pause(page, cfg, counter[0], "unfollow")
        elif res in ("gone", "already"):  # rien a cliquer : ni pause ni decompte dans les plafonds
            fan = st.followed_back(user)  # il a suivi en retour : TikTok avait bien enregistre l'abonnement
            ours = unfollow_trouble(st, user)[0] > 0  # un essai precedent "en echec" etait en fait passe
            if ours:
                st.del_meta(f"unfollow_trouble:{user}")
            st.mark_unfollowed(user, "introuvable" if res == "gone" else "deja desabonne" if fan or ours else PHANTOM,
                               action=False)
            fails = 0
            if res == "gone":
                log.info("@%s introuvable : retire de la rotation", user)
                continue
            if fan or ours or st.followed_at(user) <= since:
                log.info("@%s n'etait deja plus suivi (%s)", user, "il t'a suivi en retour" if fan else
                         "desabonnement precedent passe" if ours else "abonnement d'avant la derniere pause")
                continue
            phantoms += 1
            log.info("@%s n'etait deja plus suivi : abonnement non enregistre par TikTok ? (%d de suite)",
                     user, phantoms)
            if cfg["phantom_streak"] and phantoms >= cfg["phantom_streak"]:
                log.warning("%d abonnements de suite absents : TikTok les a ignores sans message "
                            "(limitation silencieuse probable)", phantoms)
                cfg["_block"] = "limit"
                return done, "blocked"
        else:  # 'refused' ou 'failed'
            n, first = bump_trouble(st, user)
            own = not first and (done > 0 or (st.get_meta("last_unfollow_ok_on") or "") >= yesterday)
            if own and n >= (3 if res == "refused" else 5):
                st.mark_unfollowed(user, "refus" if res == "refused" else "echec", action=False)
                log.warning("@%s : desabonnement %s %d jours, abandonne (reste suivi)", user,
                            "refuse" if res == "refused" else "en echec", n)
            if not own and res == "refused":
                refused += 1
                if refused >= 3:
                    log.warning("3 desabonnements de suite refuses par TikTok : limitation probable")
                    cfg["_block"] = "limit"
                    return done, "blocked"
            elif not own:
                fails += 1
                log.warning("echec du desabonnement de @%s (%d de suite)", user, fails)
                if fails >= 3:
                    log.warning("3 echecs de suite : interface modifiee ? lance 'tiktok_rotate.py diagnose', "
                                "voir data/debug et la skill maj-selecteurs")
                    return done, "broken"
            human_pause(page, cfg, counter[0], "unfollow")  # une page a ete ouverte, souvent un clic
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
            st.set_meta("logged_out_on", "")
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
    settle_clean_day(st)
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
    if not sessions_planned(st, False):
        print("Sessions                                   : un seul passage par jour (tache sans reveils "
              "toutes les 20 min : relance setup.ps1)")
    else:
        print(f"Sessions                                   : "
              f"{'une par partie de journee' if cfg['day_sessions'] else 'une par jour'}")
        saved = json.loads(st.get_meta("part_draw") or "{}")
        draw = saved.get("draw", {}) if saved.get("day") == today().isoformat() else {}
        seen = json.loads(st.get_meta("parts_seen") or "{}")
        since = (today() - timedelta(days=cfg["attendance_days"])).isoformat()
        past = [d for d in seen if since <= d < today().isoformat()]
        for name, opens, start, end in part_windows(cfg, today()):
            used = float(st.get_meta(f"run_seconds:{today().isoformat()}:{name}") or 0) / 60
            done = st.count("followed_at >= ? AND followed_at < ?",
                            (opens.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))
            print(f"  {name:<22} vers {start:%H:%M}  {used:4.0f} min  {done:>3} abonnement(s)"
                  + (f"  tirage du jour {draw[name] * 100:4.1f} %" if name in draw else "")
                  + (f"  PC allume {sum(name in seen[d] for d in past)}/{len(past)} j" if past else ""))
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
                missing = account_missing(page)
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


def idle_parts(st):
    """Parties du jour dont la session a epuise ses sources (pas de relance a chaque reveil)."""
    day, *parts = (st.get_meta("session_idle") or "").split("|")
    return parts if day == today().isoformat() else []


def sessions_planned(st, auto):
    """Sessions par partie de journee : en reveil automatique, ou si la tache des reveils existe (vue ces 3
    derniers jours). Sinon (ancienne tache quotidienne), un passage utilise tout le temps restant du jour."""
    return auto or (st.get_meta("last_auto_on") or "") >= (today() - timedelta(days=3)).isoformat()


def cmd_run(cfg, here, force=False, auto=False):
    """auto : reveil de la tache planifiee (toutes les 20 min) ; ne lance le navigateur que si une session est
    due, et ne journalise qu'une fois par jour chaque raison de ne rien faire."""
    st = State(DB_PATH)
    moment = now()
    windows = part_windows(cfg, today())
    cur = current_part(windows, moment)
    if auto:
        st.set_meta("last_auto_on", today().isoformat())
    if cur and moment >= cur[2]:
        mark_seen(st, cur[0])
    plan = not force and sessions_planned(st, auto)

    def say(key, msg, *args, level=logging.INFO):
        if auto:  # reveils frequents : une fois par jour et par raison
            mark = f"{today().isoformat()}:{key}"
            said = [m for m in (st.get_meta("said") or "").split("|") if m.startswith(today().isoformat())]
            if mark in said:
                return
            st.set_meta("said", "|".join(said + [mark]))
        log.log(level, msg, *args)

    settle_clean_day(st)
    attention = st.get_meta("needs_attention")
    if attention:
        say("attention", "Intervention requise (%s) : regle le probleme a la main dans TikTok, puis lance "
            "'tiktok_rotate.py login'", attention, level=logging.ERROR)
        return 5
    if auto and st.get_meta("logged_out_on") == today().isoformat():  # un essai par jour suffit
        say("deconnecte", "Session TikTok absente ou expiree : relance 'tiktok_rotate.py login'", level=logging.ERROR)
        return 3
    until = st.get_meta("blocked_until")
    if until and moment < datetime.fromisoformat(until):
        say("pause", "Pause anti-blocage jusqu'au %s : rien a faire", until)
        return 0
    if not PROFILE.exists():
        say("profil", "Aucun profil navigateur : lance d'abord 'tiktok_rotate.py login'", level=logging.ERROR)
        return 2
    if not force:
        if not in_active_hours(cfg, moment):
            say("hors", "Hors de la plage horaire %dh-%dh : rien a faire (--force pour passer outre)",
                *cfg["active_hours"])
            return 0
        if is_rest_day(cfg, today()):
            say("repos", "Jour de repos : rien a faire")
            return 0
    base = cfg["unfollow_after_days"]
    keep, keep_days = cfg["keep_followbacks"], cfg["followback_keep_days"]
    days = int(st.get_meta("current_days") or base) if cfg["adaptive_delay"] and cfg["username"] else base
    budget = follow_budget(st, cfg)
    quota = max(0, budget - st.followed_today())
    if st.following(keep) >= cfg["max_following"]:
        say("max_following", "%d comptes suivis par le script : pas de nouvel abonnement tant que les "
            "desabonnements n'ont pas rattrape leur retard", cfg["max_following"], level=logging.WARNING)
        quota = 0

    def unfollow_room():  # desabonnements encore permis aujourd'hui (aucun si l'interface a casse ce jour)
        if st.get_meta("unfollow_broken_on") == today().isoformat():
            return 0
        return max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today())
    room = unfollow_room()
    if not room and st.get_meta("unfollow_broken_on") == today().isoformat():
        say("casse", "Desabonnements en echec aujourd'hui (interface modifiee ?) : abonnements seulement "
            "jusqu'a demain ; lance 'tiktok_rotate.py diagnose'", level=logging.WARNING)
    if not quota and not (room and st.due(days, 1, keep, keep_days)):
        say("rien", "Rien a faire aujourd'hui (abonnements autorises : %d)", budget)
        return 0
    alive = [seed for seed in cfg["seeds"] if int(st.get_meta(f"seed_fail:{seed}") or 0) < 3]
    if quota and not alive:
        say("sources", "Aucune source valide : remplis seeds.txt ou lance 'tiktok_rotate.py check-seeds'",
            level=logging.ERROR)
        return 1
    left = minutes_left(st, cfg)
    if left is not None and left < 5:
        say("temps", "Temps de passage du jour deja utilise (%d min) : rien a faire", cfg["daily_minutes"])
        return 0
    part = cur[0] if cur else part_of_hour(moment.hour)
    if plan:
        if cur is None:
            say("hors", "Hors des sessions du jour : rien a faire")
            return 0
        name, _, start, end = cur
        if auto and moment < start:
            say(f"attente:{name}", "Session %s prevue vers %s", name, f"{start:%H:%M}")
            return 0
        if name in idle_parts(st):
            say(f"vide:{name}", "Session %s : sources epuisees, reprise a la session suivante", name)
            return 0
        minutes, quota, ushare = session_plan(st, cfg, windows, cur, moment, quota,
                                              len(st.due(days, room, keep, keep_days)) if room else 0)
        nxt = next((w for w in windows if w[1] >= end), None)
        if minutes < cfg["session_min_minutes"] and nxt:
            say(f"faite:{name}", "Session %s faite (temps du jour garde pour la suite) : prochaine vers %s",
                name, f"{nxt[2]:%H:%M}")
            return 0
        if not quota and not (room and st.due(days, 1, keep, keep_days)):
            say(f"vide:{name}", "Rien a faire pour la session %s", name)
            return 0
        left = minutes if left is None else min(left, minutes)
        cfg["_unfollow_share"] = ushare
        log.info("Session %s : %d min, %d abonnement(s) au plus, desabonnements d'abord sur %d %% du temps",
                 name, left, quota, ushare * 100)
    cfg["_deadline"] = now() + timedelta(minutes=left) if left is not None else None
    cfg["_block"], cfg["_recent"], cfg["_checked"], cfg["_skip"], cfg["_attempts"] = None, [], set(), set(), []
    cfg["_tried"] = set()
    cfg["_slow"], cfg["_tick"], cfg["_part"] = 1.0, now(), part
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
                watch_api(page, cfg)
                if not logged_in(page):
                    st.set_meta("logged_out_on", today().isoformat())
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
                    return bool(unfollow_room() and st.due(days, 1, keep, keep_days))

                if status == "ok" and unfollows_due():
                    t0 = now()
                    unfollows, status = unfollow_pass(page, st, cfg, counter, days, unfollow_until(cfg, quota))
                    learn_seconds(st, cfg, "unfollow", (now() - t0).total_seconds(), unfollows)
                    if status == "broken":  # plus de desabonnement aujourd'hui ; les abonnements continuent
                        st.set_meta("unfollow_broken_on", today().isoformat())
                        status = "ok"
                    # part du temps ecoulee, ou plafond horaire des desabonnements : les abonnements ont le leur
                    if status in ("later", "stop") and not day_over(cfg):
                        status = "ok"
                if status == "ok" and quota:
                    t0 = now()
                    follows, status = follow_pass(page, st, cfg, counter, alive, quota, fixed_days)
                    learn_seconds(st, cfg, "follow", (now() - t0).total_seconds(), follows)
                    if plan and status == "ok" and follows < quota:  # sources epuisees : pas de relance
                        st.set_meta("session_idle", "|".join([today().isoformat()] + idle_parts(st) + [part]))
                    if status == "stop" and not day_over(cfg):
                        status = "ok"
                if status == "ok" and unfollows_due():  # temps restant apres les abonnements
                    t0 = now()
                    n, status = unfollow_pass(page, st, cfg, counter, days)
                    learn_seconds(st, cfg, "unfollow", (now() - t0).total_seconds(), n)
                    unfollows += n
                    if status == "broken":
                        st.set_meta("unfollow_broken_on", today().isoformat())
                        status = "ok"
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception:
        log.exception("Erreur inattendue")
        return 1
    finally:
        add_run_time(st, cfg)
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
                    help="run : tout le temps restant du jour maintenant, malgre plage horaire et jour de repos")
    ap.add_argument("--auto", action="store_true",
                    help="run : reveil de la tache planifiee (sessions par partie de journee)")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "status":
        return cmd_status(cfg)
    if not acquire_lock():
        if args.auto:  # une session est en cours : ce reveil n'a rien a faire
            return 0
        log.error("Une autre instance est deja en cours")
        return 4
    if args.cmd == "login":
        return cmd_login(cfg)
    if args.cmd == "check-seeds":
        return cmd_check_seeds(cfg, args.here)
    if args.cmd == "diagnose":
        return cmd_diagnose(cfg)
    return cmd_run(cfg, args.here, args.force, args.auto)


if __name__ == "__main__":
    sys.exit(main())
