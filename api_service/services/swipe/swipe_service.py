"""Swipe: AI-picked movie and TV cards, shown one at a time and voted on.

Flow for one user:

1. ``next_batch`` asks the LLM for a batch of cards (``calibration`` mode until the
   user has cast ``CALIBRATION_TARGET`` votes, then ``normal`` mode mixing safe and
   adventurous picks around their taste profile), resolves them on TMDb, drops
   anything already voted, requested, in the library or recently served, and adds
   streaming badges and external ratings.
2. While the user swipes, the following batch is generated in a background thread
   so it is ready when the client asks for it.
3. ``vote`` stores each answer; every ``PROFILE_REFRESH_EVERY`` votes (and when
   calibration completes) the taste profile is rewritten by the LLM in the
   background.
4. ``request`` sends a liked card through the regular request queue, so the global
   approval setting and the Seer quality profiles apply.
"""

import asyncio
import threading
import time
from collections import deque

from api_service.config.logger_manager import LoggerManager
from api_service.services.request_sources import SWIPE_SOURCE
from api_service.services.swipe.signals import SwipeSignals

logger = LoggerManager.get_logger("SwipeService")

CALIBRATION_TARGET = 15
PROFILE_REFRESH_EVERY = 10
BATCH_SIZE = 10
EXPLORE_RATIO = 0.3
PROMPT_VOTES = 30
PROMPT_EXCLUDED_TITLES = 60
# Ask for more than needed: some suggestions do not resolve on TMDb or get filtered.
OVERSAMPLE = 1.5
SERVED_MEMORY = 100
PREFETCH_TTL_SECONDS = 3600
TMDB_CONCURRENCY = 5
MODES = ('auto', 'normal', 'calibration')
MEDIA_TYPES = ('movie', 'tv', 'both')


class SwipeError(Exception):
    """Invalid Swipe input (unknown vote, media type, missing card id...)."""


class _PrefetchStore:
    """Per-process store of batches generated ahead of time, and of served ids.

    Flask runs each async view in its own short-lived event loop, so background work
    runs in a daemon thread with its own loop and hands its result over here.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._batches = {}
        self._pending = set()
        self._served = {}

    def take(self, key):
        with self._lock:
            entry = self._batches.pop(key, None)
        if entry and time.monotonic() - entry['created'] < PREFETCH_TTL_SECONDS:
            return entry['result']
        return None

    def start(self, key):
        """Mark a prefetch as running; False if one is already running for *key*."""
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
            return True

    def put(self, key, result):
        with self._lock:
            self._pending.discard(key)
            if result is not None:
                self._batches[key] = {'result': result, 'created': time.monotonic()}

    def remember_served(self, user_id, keys):
        with self._lock:
            served = self._served.setdefault(user_id, deque(maxlen=SERVED_MEMORY))
            served.extend(keys)

    def served(self, user_id):
        with self._lock:
            return set(self._served.get(user_id, ()))

    def forget_user(self, user_id):
        with self._lock:
            self._served.pop(user_id, None)
            for key in [k for k in self._batches if k[0] == user_id]:
                del self._batches[key]


_store = _PrefetchStore()


def _run_in_background(coro_factory, name):
    """Run an async job in a daemon thread with its own event loop."""
    def runner():
        try:
            asyncio.run(coro_factory())
        except Exception as exc:
            logger.warning("Background swipe job %s failed: %s", name, exc)
    thread = threading.Thread(target=runner, name=f"swipe-{name}", daemon=True)
    thread.start()
    return thread


def _year(item):
    date = item.get('release_date') or item.get('first_air_date') or ''
    return int(date[:4]) if date[:4].isdigit() else None


class SwipeService:
    """Card generation, votes, requests and taste profile for one SuggestArr account."""

    def __init__(self, config=None, db=None, signals=None, store=None):
        """
        :param config: Runtime configuration; read from ``ConfigService`` when None.
        :param db: Database manager; the ``DatabaseManager`` singleton when None.
        :param signals: ``SwipeSignals``; built from *config* when None.
        :param store: Prefetch store; the process-wide one when None.
        """
        if config is None:
            from api_service.services.config_service import ConfigService
            config = ConfigService.get_runtime_config()
        if db is None:
            from api_service.db.database_manager import DatabaseManager
            db = DatabaseManager()
        self.config = config
        self.db = db
        self.signals = signals or SwipeSignals(config)
        self.store = store or _store

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def media_users(self, user):
        """Media-server users whose history belongs to this account.

        Verified linked profiles of the configured media service come first. An
        admin without a linked profile falls back to the users selected in the
        instance setup; any other account without a link gets no history.
        """
        service = str(self.config.get('SELECTED_SERVICE') or '').lower()
        try:
            profiles = self.db.get_user_media_profiles(int(user['id'])) or []
        except Exception as exc:
            logger.warning("Could not read linked media profiles: %s", exc)
            profiles = []
        linked = [
            {'id': p['external_user_id'], 'name': p.get('external_username') or p['external_user_id']}
            for p in profiles
            if str(p.get('provider') or '').lower() == service and p.get('verified')
        ]
        if linked:
            return linked
        if user.get('role') == 'admin':
            return [
                {'id': u['id'], 'name': u.get('name') or u['id']}
                for u in self.config.get('SELECTED_USERS') or []
                if isinstance(u, dict) and u.get('id')
            ]
        return []

    def language(self, user_id):
        """Language the rationales and the profile are written in."""
        from api_service.services.tmdb.localization import display_language
        try:
            own = self.db.get_user_language(int(user_id))
        except Exception:
            own = None
        return display_language(own, self.config).split('-')[0]

    def llm_configured(self, user_id):
        """True when a global or per-user OpenAI-compatible provider is set."""
        if self.config.get('OPENAI_API_KEY') or self.config.get('OPENAI_BASE_URL'):
            return True
        try:
            return bool(self.db.get_user_media_profile_token(int(user_id), 'openai'))
        except Exception:
            return False

    def status(self, user):
        """What the Swipe page needs to know before showing cards."""
        user_id = int(user['id'])
        votes = self.db.count_swipe_votes(user_id)
        return {
            'llm_configured': self.llm_configured(user_id),
            'media_history': bool(self.media_users(user)),
            'streaming_region': self.signals.streaming_region,
            'ratings_enabled': bool(self.config.get('OMDB_API_KEY')),
            'votes': votes,
            'calibration': {'done': min(votes, CALIBRATION_TARGET), 'target': CALIBRATION_TARGET,
                            'complete': votes >= CALIBRATION_TARGET},
            'profile_ready': self.db.get_taste_profile(user_id) is not None,
        }

    # ------------------------------------------------------------------
    # Batches
    # ------------------------------------------------------------------

    def _effective_mode(self, user_id, mode):
        if mode not in MODES:
            raise SwipeError(f"mode must be one of {', '.join(MODES)}")
        if mode != 'auto':
            return mode
        return 'calibration' if self.db.count_swipe_votes(user_id) < CALIBRATION_TARGET else 'normal'

    async def next_batch(self, user, media_type='both', mood=None, mode='auto', size=BATCH_SIZE):
        """Return the next cards for *user* and start preparing the batch after it.

        :param user: ``g.current_user``-style dict with 'id' and 'role'.
        :param media_type: 'movie', 'tv' or 'both'.
        :param mood: Optional free-text wish for this session.
        :param mode: 'auto' (calibration until enough votes), 'normal' or 'calibration'.
        :param size: Number of cards wanted.
        :return: Dict with 'mode', 'cards' and 'calibration' progress.
        :raises SwipeError: On invalid arguments.
        """
        if media_type not in MEDIA_TYPES:
            raise SwipeError(f"media_type must be one of {', '.join(MEDIA_TYPES)}")
        user_id = int(user['id'])
        mood = (mood or '').strip()[:200] or None
        effective = self._effective_mode(user_id, mode)
        key = (user_id, media_type, (mood or '').lower(), effective)

        cards = self.store.take(key)
        if cards is not None:
            voted = self.db.get_swipe_voted_ids(user_id)
            cards = [c for c in cards if (str(c['id']), c['media_type']) not in voted]
        if not cards:
            cards = await self.generate_batch(user, media_type, mood, effective, size)
        self.store.remember_served(user_id, [(str(c['id']), c['media_type']) for c in cards])
        self._prefetch(user, media_type, mood, effective, size, key)

        votes = self.db.count_swipe_votes(user_id)
        return {
            'mode': effective,
            'cards': cards,
            'calibration': {'done': min(votes, CALIBRATION_TARGET), 'target': CALIBRATION_TARGET},
        }

    def _prefetch(self, user, media_type, mood, mode, size, key):
        if not self.store.start(key):
            return

        async def job():
            result = None
            try:
                result = await self.generate_batch(user, media_type, mood, mode, size)
            finally:
                self.store.put(key, result)

        _run_in_background(job, f"prefetch-{key[0]}")

    async def generate_batch(self, user, media_type, mood, mode, size):
        """Ask the LLM for cards and turn them into enriched TMDb cards.

        :return: List of card dicts (TMDb fields plus rationale, pick_type,
            streaming and ratings), at most *size* long.
        """
        from api_service.services.llm.llm_service import generate_swipe_batch

        from api_service.exceptions.api_exceptions import LLMNotConfiguredError

        user_id = int(user['id'])
        if mode == 'normal' and self.db.get_taste_profile(user_id) is None:
            # The profile sharpens the batch but is not required for it.
            try:
                await self.refresh_profile(user)
            except LLMNotConfiguredError:
                raise
            except Exception as exc:
                logger.warning("Could not build the first taste profile for user %s: %s", user_id, exc)

        engagement = await self.signals.engagement(self.media_users(user))
        votes = self.db.get_swipe_votes(user_id, limit=PROMPT_VOTES)
        profile = self.db.get_taste_profile(user_id)

        suggestions = await generate_swipe_batch(
            user_id=user_id,
            count=max(size, round(size * OVERSAMPLE)),
            media_type=media_type,
            mode=mode,
            explore_ratio=EXPLORE_RATIO,
            profile_text=profile['profile_text'] if profile else None,
            engagement=engagement,
            recent_votes=votes,
            exclude_titles=self._older_voted_titles(user_id, votes),
            mood=mood,
            language=self.language(user_id),
        )

        excluded = self._excluded_keys(user_id) | await self.signals.library_keys()
        watched = {str(item['title']).strip().lower() for item in engagement if item.get('title')}
        cards = await self._resolve(suggestions, media_type, excluded, watched)
        cards = self._balance(cards, size, mode)
        await self.signals.enrich_cards(cards)
        logger.info("Swipe batch for user %s: %d/%d cards kept (mode=%s)",
                    user_id, len(cards), len(suggestions), mode)
        return cards

    def _older_voted_titles(self, user_id, recent):
        """Voted titles beyond the recent ones, so the LLM still avoids them."""
        recent_keys = {(v['tmdb_id'], v['media_type']) for v in recent}
        older = [v for v in self.db.get_swipe_votes(user_id, limit=PROMPT_VOTES + PROMPT_EXCLUDED_TITLES)
                 if (v['tmdb_id'], v['media_type']) not in recent_keys]
        return older[:PROMPT_EXCLUDED_TITLES]

    def _excluded_keys(self, user_id):
        excluded = set(self.db.get_swipe_voted_ids(user_id))
        excluded |= self.store.served(user_id)
        try:
            for tmdb_id in self.db.get_requested_tmdb_ids():
                excluded |= {(str(tmdb_id), 'movie'), (str(tmdb_id), 'tv')}
        except Exception as exc:
            logger.warning("Requested ids unavailable: %s", exc)
        return excluded

    async def _resolve(self, suggestions, media_type, excluded, watched):
        """Match LLM suggestions to TMDb items and drop the excluded ones."""
        from api_service.services.ai_search.ai_search_service import AiSearchService

        ai_search = AiSearchService.__new__(AiSearchService)
        ai_search.config = self.config
        tmdb = ai_search._make_tmdb_client()
        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)

        async def resolve(suggestion):
            kind = suggestion.get('media_type')
            if media_type != 'both':
                kind = media_type
            if kind not in ('movie', 'tv'):
                return None
            async with semaphore:
                try:
                    search = tmdb.search_movie if kind == 'movie' else tmdb.search_tv
                    results = await search(suggestion['title'], suggestion.get('year'))
                    if not results and suggestion.get('year'):
                        results = await search(suggestion['title'])
                except Exception as exc:
                    logger.warning("TMDb search failed for %r: %s", suggestion.get('title'), exc)
                    return None
            if not results:
                return None
            item = dict(results[0])
            item['media_type'] = kind
            return item, suggestion

        try:
            resolved = await asyncio.gather(*(resolve(s) for s in suggestions))
        finally:
            await tmdb.close()

        cards, seen = [], set()
        for entry in resolved:
            if not entry:
                continue
            item, suggestion = entry
            key = (str(item.get('id')), item['media_type'])
            if not item.get('id') or key in seen or key in excluded:
                continue
            if AiSearchService._is_watched(item, watched):
                continue
            if not tmdb._apply_filters(item, item['media_type'])['passed']:
                continue
            seen.add(key)
            item['year'] = _year(item)
            item['rationale'] = suggestion.get('rationale')
            item['pick_type'] = suggestion.get('pick_type') or 'safe'
            cards.append(item)
        return cards

    @staticmethod
    def _balance(cards, size, mode):
        """Keep *size* cards, preserving the safe/explore mix and interleaving them."""
        if mode == 'calibration' or len(cards) <= size:
            return cards[:size]
        explore_wanted = max(1, round(size * EXPLORE_RATIO))
        explore = [c for c in cards if c.get('pick_type') == 'explore'][:explore_wanted]
        safe = [c for c in cards if c.get('pick_type') != 'explore'][:size - len(explore)]
        if len(safe) + len(explore) < size:
            extra = [c for c in cards if c not in safe and c not in explore]
            explore += extra[:size - len(safe) - len(explore)]
        if not explore:
            return safe
        # Spread adventurous picks evenly instead of bunching them at the end.
        total = len(safe) + len(explore)
        gap = total / (len(explore) + 1)
        slots = {round(gap * (i + 1)) for i in range(len(explore))}
        safe_iter, explore_iter = iter(safe), iter(explore)
        mixed = []
        for position in range(total):
            source = explore_iter if position in slots else safe_iter
            card = next(source, None) or next(safe_iter, None) or next(explore_iter, None)
            if card is not None:
                mixed.append(card)
        return mixed

    # ------------------------------------------------------------------
    # Votes, requests, profile
    # ------------------------------------------------------------------

    def vote(self, user, card, vote):
        """Store a vote and schedule a profile refresh when enough votes piled up.

        :param card: Card dict as served by ``next_batch`` (at least id and media_type).
        :param vote: 'like', 'dislike', 'seen_liked' or 'seen_disliked'.
        :return: Dict with the stored 'vote' and 'profile_refresh' (bool).
        :raises SwipeError: On an invalid card or vote.
        """
        user_id = int(user['id'])
        tmdb_id, media_type = self._card_key(card)
        try:
            stored = self.db.set_swipe_vote(
                user_id, tmdb_id, media_type, vote,
                title=card.get('title') or card.get('name'),
                year=card.get('year') or _year(card),
                genres=card.get('genres') or None,
                rationale=card.get('rationale'),
                pick_type=card.get('pick_type'),
            )
        except ValueError as exc:
            raise SwipeError(str(exc)) from exc
        pending = self.db.increment_taste_profile_votes(user_id)
        total = self.db.count_swipe_votes(user_id)
        calibration_done = total == CALIBRATION_TARGET
        refresh = pending >= PROFILE_REFRESH_EVERY or calibration_done
        if refresh:
            _run_in_background(lambda: self.refresh_profile(user), f"profile-{user_id}")
        return {'vote': stored, 'profile_refresh': refresh}

    async def request(self, user, card):
        """Send a card through the request queue (approval setting applies).

        Also records a 'like' if the card was not voted on yet.

        :return: Dict with 'request_status': 'awaiting_approval', 'queued' or
            'already_requested'.
        """
        from api_service.config.config import load_env_vars
        from api_service.services.seer.seer_client import SeerClient, requires_request_approval

        user_id = int(user['id'])
        tmdb_id, media_type = self._card_key(card)
        if (tmdb_id, media_type) not in self.db.get_swipe_voted_ids(user_id):
            self.vote(user, card, 'like')

        config = self.config
        seer = SeerClient(
            config.get('SEER_API_URL', ''),
            config.get('SEER_TOKEN', ''),
            number_of_seasons=config.get('FILTER_NUM_SEASONS') or 'all',
            anime_profile_config=config.get('SEER_ANIME_PROFILE_CONFIG') or {},
            request_first_season_only=config.get('REQUEST_FIRST_SEASON_ONLY', False),
            queue_context={'owner_id': user_id, 'delivery_mode': 'inherit'},
        )
        media = {k: v for k, v in card.items() if k not in ('streaming', 'ratings', 'pick_type')}
        media['id'] = int(tmdb_id)
        try:
            enqueued = await seer.request_media(media_type, media, source={'id': SWIPE_SOURCE},
                                                rationale=card.get('rationale'))
        finally:
            await seer.close()
        if not enqueued:
            return {'request_status': 'already_requested'}
        self.db.mark_swipe_requested(user_id, tmdb_id, media_type)
        approval = requires_request_approval('inherit', load_env_vars().get('REQUIRE_REQUEST_APPROVAL', False))
        return {'request_status': 'awaiting_approval' if approval else 'queued'}

    async def refresh_profile(self, user):
        """Rewrite the taste profile from the votes cast since the last revision.

        :return: The new profile text, or None when there is nothing to base it on.
        """
        from api_service.services.llm.llm_service import update_taste_profile

        user_id = int(user['id'])
        current = self.db.get_taste_profile(user_id)
        pending = current['votes_since_update'] if current else PROMPT_VOTES
        new_votes = self.db.get_swipe_votes(user_id, limit=max(pending, PROFILE_REFRESH_EVERY))
        engagement = await self.signals.engagement(self.media_users(user))
        if not new_votes and not engagement and not current:
            return None
        text = await update_taste_profile(
            user_id=user_id,
            current_profile=current['profile_text'] if current else None,
            user_edited=bool(current and current['user_edited']),
            new_votes=new_votes,
            engagement=engagement,
            language=self.language(user_id),
        )
        if not text:
            return None
        # A manual edit stays flagged so later revisions keep honouring it.
        self.db.save_taste_profile(user_id, text, user_edited=bool(current and current['user_edited']))
        logger.info("Taste profile refreshed for user %s (%d chars)", user_id, len(text))
        return text

    def save_profile(self, user, profile_text):
        """Store a profile written or corrected by the user."""
        try:
            self.db.save_taste_profile(int(user['id']), profile_text, user_edited=True)
        except ValueError as exc:
            raise SwipeError(str(exc)) from exc
        return self.db.get_taste_profile(int(user['id']))

    def reset(self, user):
        """Delete the user's votes and forget what was served to them."""
        user_id = int(user['id'])
        deleted = self.db.clear_swipe_votes(user_id)
        self.store.forget_user(user_id)
        return {'deleted': deleted}

    @staticmethod
    def _card_key(card):
        if not isinstance(card, dict):
            raise SwipeError("card must be an object")
        tmdb_id, media_type = card.get('id') or card.get('tmdb_id'), card.get('media_type')
        if not str(tmdb_id or '').isdigit():
            raise SwipeError("card.id must be a TMDb id")
        if media_type not in ('movie', 'tv'):
            raise SwipeError("card.media_type must be 'movie' or 'tv'")
        return str(tmdb_id), media_type
