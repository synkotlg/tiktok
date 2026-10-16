# tiktok : rotation d'abonnements TikTok orientee follow-back

Portage TikTok du bot Instagram `insta_rotate.py` (meme architecture, memes commandes, meme setup.ps1).
Cible : Windows, Edge via Playwright (profil persistant dans `data/profile`), tache planifiee, bureau virtuel.

## Fichiers

- `tiktok_rotate.py` : tout le script, un seul fichier. Sections dans l'ordre : UI (selecteurs et textes
  FR + EN de TikTok, scripts JS), DEFAULTS, State (SQLite), statistiques de follow-back (Kaplan-Meier),
  choix des sources (Thompson), prudence, sessions de la journee (plan_session : fonctions pures), navigateur,
  profils et listes, abonnements, follow-back, desabonnements, commandes.
- `config.json` surcharge `DEFAULTS` ; `seeds.txt` = sources ; `data/` non versionne.
- `tests/test_logic.py` : logique pure, rapide. `tests/test_flows.py` + `tests/mock_tiktok.py` : parcours
  complets (run, status, diagnose) sur une maquette locale du DOM suppose, Chromium headless, ~2 min.

## Commandes

- Tests : `python -m pytest` (pytest.ini ajoute `.` et `tests` au chemin). Seulement la logique :
  `python -m pytest tests/test_logic.py`.
- Le script : `python tiktok_rotate.py login | run [--here] [--force] [--auto] | status | check-seeds |
  diagnose`. La tache planifiee (setup.ps1) lance `run --auto` toutes les 20 min ; les horaires des sessions
  viennent de config.json (`active_hours`, `day_sessions`), jamais de setup.ps1.
- Les tests de parcours passent `force=True` (pas de sessions) sauf `test_sessions`, qui fige `now`.
- Ne jamais lancer le script contre le vrai TikTok depuis une session cloud : pas de compte, et c'est le
  compte de l'utilisateur qui prend le risque. Tout se verifie sur la maquette.

## Conventions

- Messages de log, commentaires et docstrings en francais, sans accents (console Windows) ; les accents ne
  sont permis que dans les regex qui doivent coller aux textes de TikTok.
- Tout ce qui depend du DOM ou des textes de TikTok reste dans la section UI. Pas de `\b` dans les motifs
  passes au JS (en JS, `\b` ignore les lettres accentuees).
- Detection de visibilite : `visible(locator)` (tous les elements visibles), pas `.first.is_visible()`.
- Toute reaction de TikTok passe par `blocked(page, cfg)`, qui range la nature dans `cfg["_block"]`
  (`limit`, `captcha`, `warning`, `attention`) ; la politique de pause est `BLOCK_POLICY`. Les reponses de
  l'API aux (des)abonnements (lues par `watch_api`, jamais modifiees) passent par `api_reply` puis
  `api_reaction` ; l'appelant range aussi la nature dans `cfg["_block"]`. Pas de reponse vue = on se fie
  au bouton.
- `profile_state` : `follow`, `fan` (il te suit, toi non), `following`, `friends` (mutuel), None.
- Le script ne resout jamais un captcha lui-meme et ne cherche pas a masquer l'automatisation.
- Ne pas relever les quotas par defaut (`follows_per_day`, plafonds horaires, montee en charge).
- Schema SQLite : ajouter une colonne = l'ajouter au CREATE et prevoir un ALTER pour les bases existantes
  (voir la version Instagram).
- Textes de TikTok cherches dans la page : `tiktok_says(page, rx)` (ignore bio, nom et lignes de liste),
  jamais `visible(page.get_by_text(rx))` ; compte introuvable : `account_missing(page)`.
- Etat d'un passage dans `cfg` (cles `_...`), remis a zero dans `cmd_run` : `_skip`, `_attempts`, `_recent`,
  `_checked` valent pour tout le passage (une source peut etre rouverte plusieurs fois).
- Clic d'abonnement ignore ou refuse : `add_follow` puis `mark_unfollowed(PHANTOM, action=False)` (compte
  dans les plafonds, plus jamais clique, hors statistiques). Follow-back vu seulement au desabonnement :
  `mark_followed_back(late=True)` (colonne `fb_late`, censure dans `samples`).
- Desabonnement sans clic (compte disparu, abonnement deja absent) : `mark_unfollowed(..., action=False)`,
  sans `unfollowed_at`, donc hors plafonds et sans pause. Abonnement jamais enregistre par TikTok : note
  `PHANTOM`, exclu des statistiques par `REAL_FOLLOW` (a reprendre dans toute nouvelle requete de stats).
- Toute statistique de follow-back qui compare des groupes (sources, type de pseudo, heure) utilise la meme
  fenetre `seed_score_days` (`seed_table`, `scored`) et l'a priori `beta_scores`.
- Toute modification de la section UI : mettre a jour `tests/mock_tiktok.py` pour qu'elle reflete le DOM
  reel (d'apres les HTML de `data/debug` fournis par l'utilisateur), puis relancer les tests.

## Skills

- `maj-selecteurs` : TikTok a change son interface, adapter la section UI.
- `reglage-followback` : lire `status` et le journal, ajuster sources et reglages pour le follow-back.
