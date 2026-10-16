---
name: reglage-followback
description: Analyser les resultats du bot TikTok et ajuster sources et reglages pour maximiser le follow-back. A utiliser quand l'utilisateur partage la sortie de 'tiktok_rotate.py status' ou son journal, ou demande comment ameliorer son taux de follow-back.
---

# Reglage du follow-back

## Donnees a demander

- sortie de `.\.venv\Scripts\python.exe tiktok_rotate.py status` ;
- au besoin, les 200 dernieres lignes de `data/log.txt` ;
- au besoin, la base `data/state.db` (table `users` : username, followed_on, followed_back_on,
  unfollowed_on, fixed_days, source ; table `meta`).

## Lecture

- **Follow-back global** : "X % des comptes suivent en retour sous N jours". Sur TikTok, 5 a 15 % est
  courant ; sous 3 % le probleme vient presque toujours des sources ou du profil.
- **Rendement des sources** : `fb / n` = taux brut sur les comptes assez anciens ; "estime" = taux corrige du
  peu de donnees (a priori = moyenne globale, poids `seed_prior_strength`). Une source n'est jugee
  qu'apres ~50 comptes mesures.
- **Coefficient < 1 / pause anti-blocage** : TikTok a reagi recemment ; ne rien augmenter avant le retour a 1.
- **Abonnements ignores par TikTok (30 j)** : abonnements que le bouton affichait mais que TikTok n'a pas
  enregistres (limitation silencieuse). Quelques-uns : demandes refusees par des comptes prives. Plus de
  5-10 par semaine : le rythme est trop eleve pour ce compte, baisser `follows_per_day` et
  `max_follows_per_hour` de 25 %.
- **Par type de pseudo** : si "generique" rend nettement moins, le filtre s'en charge (ligne "filtre des
  pseudos generiques"). S'il rend autant ou plus, rien a faire.
- **Par moment de l'abonnement** : un ecart net (ex. soir 12 % contre matin 6 %, sur 50+ mesures chacun)
  justifie de deplacer la tache planifiee : `setup.ps1 -Time 18:30`.
- **Vitesse** : `daily_minutes` (180) limite a ~25-30 abonnements par jour, quel que soit `follows_per_day`.

## Leviers, du plus efficace au moins efficace

1. **Sources** : remplacer dans `seeds.txt` celles dont l'estime reste nettement sous la moyenne apres
   ~50 mesures ; ajouter des createurs de la meme niche, 10 k a 500 k abonnes, qui publient souvent.
   Une source qui n'apparait plus dans `seeds.txt` reste dans le classement avec "(retiree de la liste)".
2. **Profil de l'utilisateur** : bio, photo, videos du meme theme, video epinglee, publication reguliere.
3. **`keep_followbacks: true`** si l'utilisateur perd encore des abonnes apres les desabonnements malgre
   `followback_keep_days` (30 j). Contrepartie : le nombre d'abonnements augmente.
4. **Delai** : il s'ajuste seul (`adaptive_delay`). Ne forcer `unfollow_days_min` plus haut que si la courbe
   montre des follow-back tardifs (delai moyen proche de `unfollow_days_max`).
5. **Volume** en dernier, seulement apres 2 semaines sans aucune reaction de TikTok (ni abonnement ignore) :
   d'abord `daily_minutes` par paliers de 60 min (c'est lui qui limite), `follows_per_day` ensuite par
   paliers de 10 a 20. Plus de volume n'ameliore pas le taux et augmente le risque de restriction.

## A ne pas proposer

- Contourner les captchas ou masquer l'automatisation.
- Relever les plafonds horaires au-dela des defauts, ou supprimer les jours de repos.
