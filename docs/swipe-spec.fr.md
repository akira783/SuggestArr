# Spec — mode « Découverte » / Swipe (fork akira, base v2.15.0)

> **Nommage (2026-09-21)** : « discover » est déjà pris dans SuggestArr (jobs `discover_jobs`,
> source de demande `discover` libellée « Discover »). Le code interne utilise donc **`swipe`**
> (tables `swipe_*`, `services/swipe/`, blueprint `/api/swipe`, source `swipe`). Le libellé affiché
> dans l'interface reste à choisir à l'étape 4.

Statut : brouillon v2 à valider — 2026-09-21 (v2 : signaux multi-connecteurs, badge plateforme,
alternance sûr/exploration, mode Calibrage)

## Objectif

Proposer des films et séries **une carte à la fois**. L'utilisateur vote à chaque carte ;
ses votes alimentent un **profil de goûts** maintenu par l'IA, qui oriente les cartes suivantes.

## Indicateurs de succès

- **Taux de 👍 par lot** : doit augmenter au fil des votes (preuve que le profil apprend).
- **Conversion 👍 → demande**.
- Temps d'affichage de la première carte < 2 s quand un lot est déjà préchargé.

Les deux premiers sont calculables depuis `swipe_votes` (pas de télémétrie externe).

## Parcours utilisateur

0. **Premier lancement — mode Calibrage.** Tant que l'utilisateur a moins de 15 votes, le lot
   est composé de **titres très connus et variés** (grands succès récents et classiques, genres
   contrastés), pour que « Déjà vu — aimé / pas aimé » amorce le profil en quelques minutes.
   Bandeau « Calibrage : 7/15 », bouton « Passer le calibrage ». Relançable depuis le panneau profil.
1. Onglet dédié du tableau de bord (libellé à choisir). En haut : Films / Séries / Les deux, et un champ
   facultatif « envie du moment » (« SF ce soir »).
2. Une carte : affiche, titre, année, genres, note TMDb (+ IMDb / Rotten Tomatoes si OMDb est
   configuré), résumé, **« Pourquoi pour toi »** (raison IA), un badge **« Pari »** sur les cartes
   d'exploration, et un badge **« Dispo sur Netflix »** (plateformes TMDb, région configurée)
   quand le titre est déjà sur un service d'abonnement.
3. Trois actions, par bouton, par swipe (pointer events) ou au clavier :

| Action | Geste / touche | Effet |
|---|---|---|
| 👎 Pas pour moi | swipe gauche / ← | vote `dislike`, carte suivante |
| 👍 J'aime | swipe droite / → | vote `like`, puis modale **« Voulez-vous demander ce film / cette série ? »** — avec la mention « Déjà disponible sur Netflix » si c'est le cas. **Oui** → demande via le circuit normal (Requests, approbation respectée). **Non** → le like reste enregistré, rien n'est demandé |
| 👁 Déjà vu | bouton / ↓ | choix **« J'ai aimé » / « Pas aimé »** → vote `seen_liked` / `seen_disliked` |

4. Un titre ayant reçu un vote n'est **plus jamais reproposé** (à cet utilisateur).
5. Quand il reste ≤ 3 cartes, le lot suivant est préchargé en arrière-plan.
6. Panneau **« Mon profil de goûts »** : texte rédigé par l'IA, **modifiable à la main**,
   bouton « Régénérer », compteur de votes, bouton « Réinitialiser mes votes ».

## Décisions de conception

- **Tout est par utilisateur** (`auth_users.id` via `g.current_user`). L'existant AI Search
  (`ai_search_feedback`, `ai_search_seen`) est global : on ne le réutilise pas, on crée des tables dédiées.
- **Demande = `SeerClient.request_media`** (`services/seer/seer_client.py:390`) avec
  `queue_context={'owner_id': user_id, 'delivery_mode': 'inherit'}` → `awaiting_approval` si
  `REQUIRE_REQUEST_APPROVAL`, sinon `queued`. On **n'utilise pas** `/api/ai-search/request`,
  qui poste directement à Seer sans approbation ni profils qualité.
- **Source des demandes** : nouvelle valeur `swipe`, déclarée dans `services/request_sources.py`
  et `services/tmdb/localization.py`, pour distinguer ces demandes dans Requests.
- **L'IA est requise** pour ce mode en v1 (sans LLM configuré : onglet affiché avec un message
  « configurez un fournisseur IA »). Repli TMDb « similaires aux likes » = piste v2.
- **Les dislikes vont dans le prompt** (aujourd'hui l'AI Search ne les utilise qu'en exclusion par id).
- **Alternance sûr / exploration** : chaque lot = ~70 % de cartes au cœur du profil, ~30 % de
  « paris » (genre voisin, autre pays, autre époque). Le LLM marque chaque titre `pick_type:
  safe|explore`. Le ratio est réglable ; un vote sur un pari pèse davantage dans la mise à jour du
  profil (c'est là que le profil apprend le plus).
- **Connecteurs = sources de signal facultatives** : chacune n'est utilisée que si elle est
  configurée, et son absence ou sa panne ne bloque jamais un lot (dégradation silencieuse, log en
  `warning`). Aucun nouveau connecteur n'est ajouté.
- UI en **anglais**, comme le reste du projet (aucun système i18n) — facilite une future PR.

## Sources de signal (connecteurs)

| Source | Utilisation | Version |
|---|---|---|
| **Jellyfin / Emby / Plex — engagement** | Au-delà des titres `IsPlayed` (seul signal actuel, `jellyfin_client.py:157`) : **nombre d'épisodes vus par série**, **éléments en cours** (`/Items/Resume`, % de progression), nombre de lectures. Donne au LLM « très suivi / commencé puis abandonné / revu plusieurs fois ». Nouvelle méthode `get_engagement_summary(user)` côté client média, implémentée d'abord pour Jellyfin (et Emby, même API), Plex en v2 | **v1** |
| **TMDb — plateformes** | `watch/providers` pour la région configurée → badge sur la carte et mention dans la modale. Réutilise la logique de `tmdb_client.get_watch_providers` (`:718`), qui aujourd'hui ne sert qu'à exclure | **v1** |
| **Seerr — demandes passées** | Titres déjà demandés : **exclusion uniquement** (par id). Pas de signal d'intention dans le prompt : les demandes importées de Seerr n'ont ni titre ni utilisateur rattaché (constaté sur une vraie base : 83 demandes, aucune avec `user_id`) | **v1** |
| **OMDb** | Notes IMDb / Rotten Tomatoes sur la carte, si configuré | **v1** (si configuré) |
| **Trakt** | Historique et notes de l'utilisateur lié, si configuré | v2 |
| **Recherche web (SearXNG)** | Sorties récentes postérieures aux connaissances du modèle (`_get_web_search_context`) | v2 (coût en latence) |

Jellystat n'est pas un connecteur SuggestArr : il reste hors du fork.

## Profil de goûts (mémoire IA)

- Table `swipe_taste_profile` : un texte court (≤ ~1 500 caractères) structuré
  « aime / n'aime pas / nuances », + métadonnées.
- **Création** : au premier usage, générée à partir de l'historique **et du résumé d'engagement**
  (sources de signal v1) de l'utilisateur (profils média liés → `user_ids`), puis affinée par le
  mode Calibrage.
- **Mise à jour** : tous les **10 votes** (réglable), appel LLM « voici le profil actuel +
  les nouveaux votes → renvoie le profil révisé ». Exécutée en tâche de fond après le vote, sans
  bloquer l'UI.
- **Édition manuelle** : si l'utilisateur a modifié le texte, la mise à jour suivante reçoit la
  consigne « respecte les corrections de l'utilisateur, ne les contredis pas ».
- Le profil est injecté dans chaque prompt de lot, avec les 30 derniers votes.

## Backend

### Tables (ajoutées dans `db/components/schema_manager.py` + branche MySQL dans `_prepare_create_table_query_for_db`)

```sql
-- swipe_votes reçoit aussi pick_type TEXT (safe | explore | calibration)
swipe_votes(
  user_id INTEGER NOT NULL,           -- auth_users.id
  tmdb_id TEXT NOT NULL,
  media_type TEXT NOT NULL,           -- movie | tv
  vote TEXT NOT NULL,                 -- like | dislike | seen_liked | seen_disliked
  title TEXT, year INTEGER, genres TEXT,   -- pour le prompt, sans rappeler TMDb
  rationale TEXT,
  requested INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, tmdb_id, media_type)
)

swipe_taste_profile(
  user_id INTEGER PRIMARY KEY,
  profile_text TEXT NOT NULL,
  votes_since_update INTEGER DEFAULT 0,
  user_edited INTEGER DEFAULT 0,
  updated_at TIMESTAMP
)
```

Accès via un nouveau `db/components/swipe_mixin.py`, ajouté à `DatabaseManager`
(`db/database_manager.py:39`). Idiome placeholders `?`/`%s` des autres mixins.

### Service `services/swipe/swipe_service.py`

- `next_batch(user_id, media_type, mood=None, size=10)` :
  historique (réutilise `AiSearchService._get_history`) + résumé d'engagement, profil, 30 derniers
  votes, exclusions (votes existants, `get_requested_tmdb_ids`, titres vus dans Jellyfin) → LLM
  (mode normal ou Calibrage selon le nombre de votes) → résolution TMDb (réutilise
  `_resolve_suggested_title`, filtres TMDb et `_apply_suggestion_rationales`) → enrichissement
  facultatif en parallèle (plateformes TMDb, OMDb).
- **Préchargement** : le lot suivant est calculé côté serveur dès qu'un lot est servi et gardé en
  cache par utilisateur (mémoire du process, invalidé si le profil change), pour une ouverture
  d'onglet instantanée.
- `vote(user_id, item, vote)` : upsert du vote, incrémente `votes_since_update`, déclenche
  `refresh_profile` au seuil.
- `request(user_id, item)` : `SeerClient.request_media(..., source={'id': 'swipe'})`, puis
  `requested=1`.
- `refresh_profile(user_id, force=False)`.

### LLM (`services/llm/llm_service.py` + `schemas.py`)

- `generate_swipe_batch(..., mode='normal'|'calibration', explore_ratio=0.3)` → schéma
  `SwipeBatch{items:[{title, year, media_type, rationale, pick_type}]}` (`pick_type` :
  `safe` | `explore`).
- `update_taste_profile(current, votes, user_edited)` → schéma `TasteProfile{profile_text}`.
- Passent par `_call_with_validation` ; `get_llm_client(user_id)` pour profiter de la config IA
  par utilisateur déjà supportée.

### Routes — blueprint `blueprints/swipe/routes.py`, préfixe `/api/swipe`

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/next?media_type=&mood=` | lot de cartes |
| POST | `/vote` | `{tmdb_id, media_type, vote, metadata}` |
| POST | `/request` | `{tmdb_id, media_type, metadata}` → file Requests |
| GET / PUT | `/profile` | lire / modifier le profil (PUT pose `user_edited=1`) |
| POST | `/profile/refresh` | régénérer |
| DELETE | `/votes` | réinitialiser ses votes |

Limites de débit comme l'AI Search (`@limiter.limit`). Onglet `swipe` ajouté à
`_VALID_VISIBLE_TABS` (`blueprints/users/routes.py:45`) et au défaut du mode bypass
(`auth/middleware.py:317`).

## Frontend

- `client/src/components/SwipePage.vue` (Options API, comme `AiSearchPage.vue`), onglet
  enregistré dans `DashboardPage.vue` (`tabs`, `componentMap`, imports).
- Swipe fait main en pointer events (aucune lib ajoutée) : seuil ~30 % de largeur, rotation légère,
  retour élastique si relâché avant le seuil.
- Modales en `teleport` + `modal-fade` sur les primitives `primitives/modal.css`.
- Fonctions API dans `client/src/api/swipeApi.js`.
- Styles `client/src/assets/styles/swipePage.css` sur les tokens de `variables.css`.

## Tests

- Mixin : SQLite en mémoire (modèle `test_suggestion_feedback_and_limits.py`).
- Service : `IsolatedAsyncioTestCase`, LLM et TMDb patchés (modèle `test_ai_search_service.py`) —
  exclusions, seuil de mise à jour du profil, respect de `user_edited`.
- Routes : `test_request_context` + `g.current_user` (modèle `test_ai_search_routes.py`) —
  isolation entre utilisateurs, statut `awaiting_approval` quand l'approbation est requise.
- Front : `node --test` pour la logique de swipe/pile (fonctions pures).

## Déploiement (fork)

- Image construite localement : `docker build -f docker/Dockerfile --target prod -t suggestarr-akira:2.15.0-swipe.1 .`
- Compose `/media/arr/suggestarr` : `image:` remplacée ; retour arrière = remettre
  `ciuse99/suggestarr:v2.15.0`. Migration **uniquement additive** (2 nouvelles tables) → retour
  arrière sans risque. Sauvegarde de `config_files/` avant le premier déploiement.
- Mise à jour upstream : rebase de la branche `feature/discover` sur chaque tag.

## Lots de travail

1. Tables + mixin + tests.
2. Sources de signal v1 : engagement Jellyfin/Emby, plateformes TMDb, OMDb facultatif + tests.
3. Service (normal, Calibrage, sûr/exploration, préchargement) + prompts LLM + routes + tests.
4. Page Swipe (cartes, badges, swipe, modales, bandeau Calibrage).
5. Profil de goûts (panneau, édition, régénération).
6. Build de l'image, déploiement, essai réel, lecture des indicateurs après ~100 votes.

## Hors périmètre v1

Trakt et recherche web comme sources (v2) ; engagement Plex (v2) ; repli sans IA ; bande-annonce sur la carte ; choix du profil qualité dans la modale de demande
(`ApprovalProfileChoice.vue` réutilisable plus tard) ; correction du `tvdbId = tmdb_id` de
`/api/ai-search/request` (bug upstream à signaler séparément).

## Décisions prises pendant l'implémentation

- **Nom interne `swipe`** (voir l'encadré en tête) ; source de demande `swipe`.
- **Historique d'un compte** : profils média liés et vérifiés ; un admin sans lien retombe sur
  les utilisateurs sélectionnés dans la config de l'instance ; tout autre compte sans lien n'a
  pas d'historique (jamais celui d'un autre).
- **PlayCount non fiable** (lecture debrid : chaque relance compte) → jamais utilisé comme
  signal de revisionnage.
- **Une série n'est « abandonnée »** que si peu d'épisodes ont été vus (≤ 5), pas sur la seule
  durée de pause.
- **Le premier profil est facultatif pour un lot** : s'il échoue, le lot part sans profil.
- **Préchargement** dans un thread avec sa propre boucle asyncio (Flask exécute chaque vue async
  dans une boucle éphémère) ; un seul préchargement à la fois par (compte, type, envie, mode).
- **Genres dans le prompt + poids des preuves** : sans genres, le LLM prenait un documentaire
  pour de l'action ; un film vu une fois est un signal faible.
- Réponse de `/api/swipe/request` : champ `request_status` (`awaiting_approval`, `queued`,
  `already_requested`), distinct du `status` générique des réponses.
