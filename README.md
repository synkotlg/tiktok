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

La tache planifiee `TikTokRotate` reveille le script toutes les 20 min (`run --auto`) et 5 min apres chaque
ouverture de session. A chaque reveil, il decide lui-meme s'il fait une session (voir **Sessions**) ; sinon
il s'arrete en moins d'une seconde, sans ouvrir le navigateur. Le PC n'est jamais sorti de veille.
Options : `-Every 30`, `-DailyOnly` (pas de declenchement a l'ouverture de session), `-NoTask`, `-Remove`.
Apres une mise a jour du script, relance `setup.ps1` : l'ancienne tache (un passage a 09:00) marche encore,
mais sans sessions.

## Commandes

| Commande | Role |
|---|---|
| `login` | connexion manuelle (code, captcha) ; a refaire si la session expire |
| `run` | une session ; `--here` garde la fenetre sur ton bureau, `--force` fait tout le temps restant du jour maintenant (malgre plage horaire et repos) ; `--auto` = reveil de la tache planifiee |
| `status` | etat de la rotation, taux de follow-back, classement des sources |
| `check-seeds` | visite chaque source et ecarte celles qui n'existent pas |
| `diagnose` | teste chaque selecteur sur ton profil et la premiere source ; captures et HTML dans `data/debug` |

## Maximiser le follow-back

Ce que fait le script :

- **Choix des sources par rendement.** Chaque abonnement est rattache a sa source. Le taux de follow-back
  de chaque source (sous `seed_score_days` jours, la meme fenetre pour toutes) est estime avec un a priori
  egal a la moyenne globale, puis les sources sont ordonnees par echantillonnage de Thompson : la meilleure
  passe le plus souvent en premier, une source peu testee garde sa chance. `status` affiche le classement.
- **Abonnes recents, liste rouverte.** TikTok liste les abonnes d'un compte du plus recent au plus ancien :
  le script prend le haut de la liste, par tranches de `follows_per_seed` (10). Apres chaque tranche la
  source est tiree a nouveau ; si c'est la meme, sa liste rouverte montre les comptes qui viennent de la
  suivre, donc en ligne a cet instant : ils voient ta notification tout de suite. Les tranches reparties
  sur plusieurs sources font aussi apprendre plus vite laquelle rend le mieux.
- **Pseudos generiques evites s'ils rendent moins.** Les comptes au pseudo attribue d'office par TikTok
  (`user` + chiffres) regardent souvent sans publier. Leur follow-back est mesure a part (`status`) ; des
  que chaque groupe a `handle_filter_min` (40) mesures, ils sont ecartes s'ils rendent nettement moins
  (moins de `handle_filter_ratio` = 70 % du taux des autres), sauf 10 % gardes pour continuer a mesurer.
  Ecarter un compte coute un peu de defilement ; un abonnement coute plusieurs minutes du plafond horaire.
- **Pas de doublon inutile.** Les comptes qui te suivent deja ("Suivre en retour") sont ignores : ils ne
  rapporteraient rien.
- **Delai adaptatif.** Le delai de desabonnement est le plus court qui capte 90 % des follow-back observes ;
  20 % des comptes (groupe temoin) restent suivis le delai maximal pour continuer a mesurer.
- **Ratio abonnements / abonnes bas.** Les desabonnements reguliers gardent ton nombre d'abonnements faible :
  un profil qui suit 5 000 comptes inspire moins le follow-back.
- **Follow-back gardes** : se desabonner juste apres un follow-back pousse le compte a se desabonner a son
  tour. Ceux qui ont suivi en retour restent suivis au moins `followback_keep_days` (30) jours, et passent
  apres tous les autres dans la file des desabonnements. **`keep_followbacks`** (false par defaut) : a true,
  le script ne s'en desabonne jamais.
- **Follow-back rattrapes au desabonnement** : la lecture de tes abonnes est plafonnee
  (`followback_scan_rows`) et peut en manquer. Avant de cliquer, le script regarde le bouton du profil :
  "Amis" (ou "Suivre en retour") = il te suit. Le follow-back est note, et le compte garde comme les autres.
- **Moment de la journee** : le follow-back est mesure selon l'heure des abonnements (matin, apres-midi,
  soir ; `status`), et les abonnements vont d'abord aux sessions des moments qui rendent le mieux (voir
  **Sessions**).

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
| `ramp_window_days` | 14 | seuls les jours actifs de cette fenetre comptent : apres une absence, la montee en charge reprend |
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

**Limitation silencieuse.** Le premier signe d'une restriction sur TikTok est souvent un abonnement que le
bouton affiche "Abonne" mais que TikTok n'enregistre pas. Le script le cherche de deux facons :

- a la fin de chaque tranche (au plus `verify_every` = 10 abonnements ; 3 apres une pause ou un captcha
  resolu), il ouvre le profil d'un compte suivi dans la tranche : si l'abonnement n'y figure pas, il en
  verifie un second ; deux absents = pause de 48 h, quotas x 0.5 (comme "Trop de tentatives") ;
- un clic ignore ou refuse est note (il compte dans les plafonds, le compte n'est plus clique) : 3 de
  suite, ou 4 sur les 12 derniers essais = meme pause ;
- au desabonnement, un compte deja plus suivi est note "non pris en compte" (sans clic, sans pause, hors
  plafonds) ; `phantom_streak` (5) de suite = meme pause. Seuls comptent les abonnements faits depuis la
  derniere pause : ceux d'avant l'ont deja provoquee.

Ces abonnements fantomes sont exclus des statistiques (le compte n'a jamais ete suivi, il ne pouvait rien
rendre) et comptes dans `status` ("Abonnements ignores par TikTok").

**Reponse de TikTok.** A chaque (des)abonnement, la page envoie une requete a TikTok ; le script lit sa
reponse (lecture seule). Un refus avec un message reconnu ("Trop de tentatives", "activite inhabituelle")
vaut blocage immediat ; un refus sans message compte comme un abonnement non pris en compte (3 de suite =
blocage), meme si le bouton affiche "Abonne". Si cette reponse n'est jamais vue (adresse changee), le
journal le signale et le controle se fait par le bouton seul, comme avant.

**Desabonnements refuses ou en echec** : le compte passe en fin de file, et son compteur monte au plus une
fois par jour. Si les desabonnements marchent pour les autres comptes, son ennui lui est propre : il ne
compte pas dans les series, et apres 3 jours de refus (5 d'echec) le compte est abandonne (il reste suivi).
Sinon, tout compte : 3 refus = pause ; 3 echecs = interface modifiee, plus de desabonnement jusqu'au
lendemain (les abonnements continuent ; lance `diagnose`). Ainsi un meme compte en tete de file ne bloque
pas chaque passage, et une interface cassee n'entraine aucun abandon.

**Session TikTok expiree** : un essai, puis plus aucun lancement d'Edge de la journee (un message par jour
dans le journal) jusqu'a `login`.

**Credit d'un jour propre** (+10 points de quotas apres une pause) : compte une fois le jour fini, sans
reaction de TikTok dans aucune de ses sessions.

**Textes des profils** : une bio ou un nom qui contient "trop rapidement", "Try again later" ou "Ce compte
est introuvable" n'est pas pris pour un message de TikTok (seuls comptent les textes hors bio, nom et
lignes de liste ; un compte n'est introuvable que si son profil ne s'affiche pas).

**Apres un captcha resolu**, les pauses sont multipliees par 1.5 jusqu'a la fin du passage (x 3 au plus).

**Apres une absence** (PC eteint, vacances, longue pause), la montee en charge reprend depuis `ramp_start` :
seuls comptent les jours d'abonnement des `ramp_window_days` (14) derniers jours.

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
| `seeds_per_run` | 6 | sources epuisees ou inaccessibles au plus par passage |
| `follows_per_seed` | 10 | abonnements d'affilee dans une meme liste avant de tirer a nouveau la source |
| `followback_keep_days` | 30 | follow-back gardes au moins ce nombre de jours (0 : meme delai que les autres) |
| `handle_filter` | true | ecarte les pseudos generiques s'ils rendent nettement moins (`handle_filter_min` 40, `handle_filter_ratio` 0.7, `handle_filter_explore` 0.1) |
| `unfollow_time_share` | 0.5 | part du temps du jour au plus pour les desabonnements tant qu'il reste des abonnements a faire |
| `verify_every`, `phantom_streak` | 10, 5 | detection de la limitation silencieuse (0 : desactive) |
| `day_sessions` | true | une session par partie de journee ; false : une par jour |
| `session_jitter_minutes` | 45 | debut de chaque session tire au hasard dans ces minutes |
| `session_min_minutes` | 15 | session plus courte ignoree (sauf la derniere de la journee) |
| `attendance_days` | 14 | jours observes pour prevoir si le PC sera allume aux sessions suivantes |
| `virtual_desktop` | 2 | bureau virtuel du navigateur (peut etre partage avec le bot Instagram) |
| `browser_channel` | msedge | navigateur ; repli sur le Chromium de Playwright |
| `locale`, `timezone` | fr-FR, Europe/Paris | langue et fuseau du navigateur |

Toutes les cles et leurs valeurs par defaut sont dans `DEFAULTS` en tete de `tiktok_rotate.py` ; `config.json`
ne contient que celles que tu veux changer.

## Sessions

Au lieu d'un bloc de 3 h, le script fait une session par partie de la journee dans `active_hours` : matin
(8-12 h), apres-midi (12-18 h), soir (18-23 h). Chaque session commence a une heure tiree au hasard dans les
45 premieres minutes de sa partie (differente chaque jour).

- **Temps** : `daily_minutes` est reparti entre les sessions. Chaque session suivante garde sa part si le PC
  est d'habitude allume a ce moment (mesure sur 14 jours ; au debut, une demi-part) ; la derniere de la
  journee prend tout ce qui reste. Un PC allume seulement le soir fait donc tout le soir : pas de temps perdu.
- **Abonnements au meilleur moment** : le follow-back est mesure par partie de journee. Chaque jour, un
  tirage (Thompson, comme pour les sources) classe les parties : la mieux classee recoit les abonnements en
  premier, les autres font surtout les desabonnements (qui ne dependent pas de l'heure). Une partie moins
  bonne garde sa chance d'etre mesuree.
- **Reveils** : la tache tourne toutes les 20 min ; un reveil hors session ne fait rien (et ne le note
  qu'une fois par jour dans le journal). Une session coupee (PC eteint) reprend au reveil suivant dans la
  meme partie, avec ce qui lui reste : le temps est releve avant chaque action (un PC en veille, couvercle
  ferme, ne consomme pas la journee). Une session dont les sources sont epuisees n'est pas relancee avant
  la partie suivante ; une source inaccessible ne prend qu'un echec par jour (3 jours pour l'ecarter).

`status` affiche les sessions du jour : debut prevu, minutes utilisees, abonnements, tirage du jour, et
combien de jours le PC etait allume a ce moment. `run` lance a la main fait la session en cours ;
`run --force` fait tout le temps restant du jour, d'un coup.

## Vitesse reelle

Le debit est fixe par le rythme (une action toutes les 3 min environ, pauses longues comprises : ~17
abonnements ou ~20 desabonnements par heure) et par `daily_minutes`. Avec 180 min par jour, une fois la
rotation lancee, cela fait environ 25 a 30 abonnements et autant de desabonnements par jour :
`follows_per_day` (80) n'est pas atteint. Les sessions ne changent pas ce total, seulement le moment des
abonnements. Les desabonnements en attente prennent au plus la moitie du temps de la journee
(`unfollow_time_share`). Un desabonnement sans clic (compte disparu, abonnement deja absent) ne coute ni
pause ni place dans les plafonds.

Pour aller plus vite sans augmenter le rythme horaire, il faut plus de temps par jour (`daily_minutes`) :
c'est plus d'actions par jour, donc plus de risque. A ne faire qu'apres 2 semaines sans aucune reaction de
TikTok, par paliers de 60 min.

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
