# tiktok

Rotation d'abonnements TikTok orientee follow-back : meme principe que `insta_rotate.py`, adapte a TikTok.

Chaque jour, le script :

1. lit la liste de tes abonnes et note qui, parmi les comptes qu'il a suivis, t'a suivi en retour ;
2. ajuste le delai de desabonnement d'apres ces mesures (Kaplan-Meier, comme la version Instagram) ;
3. se desabonne des comptes suivis depuis ce delai ;
4. s'abonne a de nouveaux comptes pris dans les abonnes recents de comptes sources (`seeds.txt`),
   en privilegiant les sources dont les abonnes suivent le plus en retour.

Le navigateur (Edge) utilise un profil persistant : tu te connectes une seule fois a la main. Les passages
tournent sur un bureau virtuel Windows dedie, sans te gener.

Attention : l'automatisation des abonnements est contraire aux conditions d'utilisation de TikTok. Le script
reste prudent (montee en charge, plafonds horaires, jours de repos, pause et quotas reduits a la moindre
reaction de TikTok), mais le risque de restriction du compte existe. A utiliser en connaissance de cause.

## Installation (Windows)

```powershell
cd chemin\vers\tiktok
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Puis, une seule fois :

1. remplis `seeds.txt` (voir plus bas) ;
2. `.\.venv\Scripts\python.exe tiktok_rotate.py login` : connecte-toi dans la fenetre, puis Entree.
   Ton pseudo est detecte et enregistre dans `config.json` ;
3. `.\.venv\Scripts\python.exe tiktok_rotate.py diagnose` : verifie que le script reconnait l'interface
   (aucun abonnement n'est fait). Si une ligne affiche 0, None ou `hidden`, voir Depannage ;
4. premier passage visible pour controler : `.\.venv\Scripts\python.exe tiktok_rotate.py run --here --force`.

La tache planifiee `TikTokRotate` lance ensuite un passage chaque jour a 09:00 (plus un delai aleatoire)
et 5 min apres chaque ouverture de session. Options : `-Time 18:30`, `-DailyOnly`, `-NoTask`, `-Remove`.

## Commandes

| Commande | Role |
|---|---|
| `login` | connexion manuelle (code, captcha) ; a refaire si la session expire |
| `run` | passage quotidien ; `--here` garde la fenetre sur ton bureau, `--force` ignore plage horaire et repos |
| `status` | etat de la rotation, taux de follow-back, classement des sources |
| `check-seeds` | visite chaque source et ecarte celles qui n'existent pas |
| `diagnose` | teste chaque selecteur sur ton profil et la premiere source ; captures et HTML dans `data/debug` |

## Maximiser le follow-back

Ce que fait le script :

- **Choix des sources par rendement.** Chaque abonnement est rattache a sa source. Le taux de follow-back
  de chaque source (sous `seed_score_days` jours, la meme fenetre pour toutes) est estime avec un a priori
  egal a la moyenne globale, puis les sources sont ordonnees par echantillonnage de Thompson : la meilleure
  passe le plus souvent en premier, une source peu testee garde sa chance. `status` affiche le classement.
- **Abonnes recents.** TikTok liste les abonnes d'un compte du plus recent au plus ancien : le script prend
  le haut de la liste, donc des comptes actifs.
- **Pas de doublon inutile.** Les comptes qui te suivent deja ("Suivre en retour") sont ignores : ils ne
  rapporteraient rien.
- **Delai adaptatif.** Le delai de desabonnement est le plus court qui capte 90 % des follow-back observes ;
  20 % des comptes (groupe temoin) restent suivis le delai maximal pour continuer a mesurer.
- **Ratio abonnements / abonnes bas.** Les desabonnements reguliers gardent ton nombre d'abonnements faible :
  un profil qui suit 5 000 comptes inspire moins le follow-back.
- **`keep_followbacks`** (false par defaut, comme la version Instagram) : a true, le script reste abonne aux
  comptes qui ont suivi en retour, pour ne pas les perdre quand il se desabonne.

Ce qui depend de toi :

- des sources de ta niche, de taille moyenne, actives (conseils dans `seeds.txt`) ; remplace celles qui
  restent en bas du classement apres 2-3 semaines ;
- un profil qui donne envie : photo, bio claire, au moins 6-9 videos dans le meme theme, une video epinglee ;
- publier regulierement pendant la rotation : les comptes suivis viennent voir ton profil, ce qu'ils y
  trouvent decide du follow-back.

## Prudence

| Cle | Defaut | Role |
|---|---|---|
| `follows_per_day` | 80 | plafond quotidien d'abonnements |
| `ramp_start`, `ramp_step` | 15, 10 | montee en charge : 15 le premier jour actif, +10 par jour actif |
| `max_follows_per_hour` / `max_unfollows_per_hour` | 20 / 25 | rythme, cumule sur tous les passages |
| `max_unfollows_per_day` | 100 | plafond quotidien de desabonnements |
| `max_following` | 1000 | plus de nouvel abonnement au-dela (comptes suivis par le script) |
| `active_hours` | [8, 23] | plage horaire |
| `daily_minutes` | 180 | temps de passage maximal par jour |
| `rest_day_chance` | 0.15 | jours de repos aleatoires, jamais deux de suite |
| `captcha_wait_minutes` | 10 | temps laisse pour resoudre un captcha a la main (bureau virtuel du bot) |
| `captcha_cooldown_hours` | 12 | pause apres un captcha non resolu, quotas x 0.75 |
| `block_cooldown_hours` | 48 | pause apres "Trop de tentatives" ou clics ignores, quotas x 0.5 |
| `warning_cooldown_hours` | 168 | pause apres un avertissement d'activite suspecte, quotas x 0.25 |

Chaque pause double si les blocages se rapprochent (14 jours au plus). Les quotas remontent de 10 points par
jour sans incident. Compte banni, suspendu ou deconnecte : arret total jusqu'a `login`.

Le script ne resout jamais un captcha lui-meme : il attend que tu le fasses (bascule sur le bureau virtuel 2),
sinon il se met en pause.

## Autres reglages

| Cle | Defaut | Role |
|---|---|---|
| `unfollow_after_days` | 4 | delai de depart (puis adaptatif entre `unfollow_days_min` 2 et `unfollow_days_max` 7) |
| `adaptive_delay` | true | ajustement du delai d'apres les follow-back mesures |
| `seed_scoring` | true | false : simple rotation dans l'ordre de `seeds.txt` |
| `seed_score_days` | 2 | fenetre de follow-back pour classer les sources |
| `seed_prior_strength` | 20 | poids de la moyenne globale dans l'estimation d'une source |
| `seed_dry_days` | 3 | une source epuisee passe en dernier pendant ce nombre de jours |
| `seeds_per_run` | 6 | sources essayees au plus par passage |
| `virtual_desktop` | 2 | bureau virtuel du navigateur (peut etre partage avec le bot Instagram) |
| `browser_channel` | msedge | navigateur ; repli sur le Chromium de Playwright |
| `locale`, `timezone` | fr-FR, Europe/Paris | langue et fuseau du navigateur |

Toutes les cles et leurs valeurs par defaut sont dans `DEFAULTS` en tete de `tiktok_rotate.py` ; `config.json`
ne contient que celles que tu veux changer.

## Depannage

- **Interface modifiee** (desabonnements en echec, listes inaccessibles, `diagnose` qui affiche 0) : tous
  les selecteurs sont dans la section UI de `tiktok_rotate.py`. `diagnose` liste les attributs `data-e2e`
  presents, les textes des boutons et enregistre le HTML des listes dans `data/debug`. La skill Claude
  `maj-selecteurs` decrit la marche a suivre.
- **Session expiree** : `tiktok_rotate.py login`.
- **Journal** : `data/log.txt` ; captures d'ecran des incidents dans `data/debug`.

## Fichiers

| Fichier | Role |
|---|---|
| `tiktok_rotate.py` | le script |
| `config.json` | tes reglages (surchargent `DEFAULTS`) |
| `seeds.txt` | comptes sources |
| `setup.ps1` | installation et tache planifiee |
| `data/` | profil du navigateur, base `state.db`, journal, captures (non versionne) |
| `tests/` | tests : logique pure, et parcours complets sur une maquette locale de TikTok |

Tests : `pip install pytest` puis `python -m pytest` (les parcours complets demandent un Chromium pour
Playwright, sinon ils sont ignores).
