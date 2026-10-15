---
name: maj-selecteurs
description: Adapter tiktok_rotate.py quand TikTok change son interface. A utiliser quand le journal signale des desabonnements en echec, des listes d'abonnes inaccessibles, des abonnements "non pris en compte" sans message de TikTok, ou quand 'diagnose' affiche 0, None ou hidden.
---

# Mise a jour des selecteurs TikTok

Tout ce qui depend du DOM ou des textes de TikTok est dans la section UI en tete de `tiktok_rotate.py`.
On ne touche qu'a cette section (et a la maquette des tests).

## 1. Obtenir le DOM reel

Demander a l'utilisateur de lancer sur son PC :

```
.\.venv\Scripts\python.exe tiktok_rotate.py diagnose
```

puis de fournir : les lignes du journal (`data/log.txt`) entre "Diagnostic de l'interface TikTok" et
"Captures et HTML", les fichiers `data/debug/*-diag-list-*.html` et les captures `*-diag-*.png`.
Ne jamais lancer le script soi-meme contre le vrai TikTok.

## 2. Lire le rapport

| Ligne du rapport | Si faux, a modifier |
|---|---|
| connecte / bouton de connexion | `LOGGED_IN_SEL`, `LOGIN_SEL` |
| pseudo detecte | `NAV_PROFILE_SEL` |
| titre (USER_TITLE_SEL) = 0 | `USER_TITLE_SEL` (sert aussi a trouver le bouton du profil) |
| compteur d'abonnes = 0 | `FOLLOWERS_COUNT_SEL`, `FOLLOWERS_LABEL_SEL` |
| bouton du profil None ou mauvais etat | `JS_PROFILE_BTN`, `NOT_FOLLOWING_RE`, `FOLLOWING_RE` |
| liste d'abonnes `hidden` alors qu'elle s'affiche | `DIALOG_SEL`, `ROW_LINK_SEL`, `FOLLOWERS_TAB_RE` |
| comptes dans la liste = 0 | `ROW_LINK_SEL`, regex des liens dans `JS_USERS` et `JS_HREF_USERS` |
| textes des boutons de la liste | comparer a `FOLLOW_RE`, `FOLLOW_BACK_RE`, `FOLLOWING_RE` |
| boutons Suivre -> pseudo avec des null | structure des lignes : `JS_USERS` |
| defilement N -> N alors que la liste est longue | `scroll_dialog`, `JS_SCROLLER` |

La ligne "data-e2e de la page" liste les attributs `data-e2e` presents : les preferer aux classes CSS
(generees, elles changent a chaque deploiement de TikTok).

## 3. Modifier

- Ajouter les nouveaux textes aux regex existantes plutot que remplacer (FR et EN, anciennes et nouvelles
  variantes) ; garder les ancres `^...$` des boutons pour ne pas confondre "Suivre" et "Suivre en retour".
- Pas de `\b` dans `NOT_FOLLOWING_RE` / `FOLLOWING_RE` : elles sont aussi executees en JS.
- Mettre a jour `tests/mock_tiktok.py` pour qu'elle reproduise le nouveau DOM (structure des lignes,
  fenetre, textes), puis les assertions de `tests/test_logic.py::test_regexes` si besoin.

## 4. Verifier

```
python -m pytest
```

Puis demander a l'utilisateur : `tiktok_rotate.py diagnose`, et un passage visible
`tiktok_rotate.py run --here --force` en surveillant les premieres actions.
