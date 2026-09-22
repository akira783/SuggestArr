"""Tests for SwipeService: batches, exclusions, prefetch, votes, requests, profile."""

import logging
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from api_service.db.components.schema_manager import SchemaManager
from api_service.exceptions.api_exceptions import LLMValidationError
from api_service.db.components.swipe_mixin import SwipeMixin
from api_service.services.swipe import swipe_service as svc
from api_service.services.swipe.swipe_service import (
    CALIBRATION_TARGET,
    PROFILE_REFRESH_EVERY,
    SwipeError,
    SwipeService,
    _PrefetchStore,
)

ADMIN = {'id': '1', 'role': 'admin'}
MEMBER = {'id': '2', 'role': 'user'}
CONFIG = {
    'SELECTED_SERVICE': 'jellyfin',
    'SELECTED_USERS': [{'id': 'jf-akira', 'name': 'akira'}],
    'OPENAI_API_KEY': 'key',
    'TMDB_LANGUAGE': 'fr',
    'SEER_API_URL': 'http://seerr:5055',
    'SEER_TOKEN': 'token',
    'SEER_ANIME_PROFILE_CONFIG': {'default_movie': {'profileId': 9}},
}


class Db(SwipeMixin):
    db_type = 'sqlite'

    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.logger = logging.getLogger('test_swipe_service')
        self.media_profiles = {}
        self.requested = set()

    def get_connection(self):
        return self.connection

    def get_user_media_profiles(self, user_id):
        return self.media_profiles.get(user_id, [])

    def get_user_language(self, user_id):
        return None

    def get_requested_tmdb_ids(self):
        return set(self.requested)

    def get_user_media_profile_token(self, user_id, provider):
        return None

    def get_auth_user_by_id(self, user_id):
        return {'id': user_id, 'role': 'admin' if user_id == 1 else 'user', 'is_active': True}


def _db():
    db = Db()
    SchemaManager(db).initialize_db()
    for user_id in (1, 2):
        db.connection.execute(
            "INSERT INTO auth_users (id, username, password_hash) VALUES (?, ?, 'x')",
            (user_id, f'u{user_id}'),
        )
    db.connection.commit()
    return db


class FakeSignals:
    streaming_region = 'FR'

    def __init__(self, engagement=None, library=None):
        self._engagement = engagement or []
        self._library = library or set()
        self.engagement_users = []

    async def engagement(self, users, limit=40):
        self.engagement_users.append(users)
        return list(self._engagement)

    async def library_keys(self):
        return set(self._library)

    async def enrich_cards(self, cards):
        for card in cards:
            card['streaming'] = None
            card['ratings'] = None
        return cards


class FakeTmdb:
    """Resolves titles from a small catalogue; items flagged 'fail' fail the filters."""

    def __init__(self, catalogue):
        self.catalogue = catalogue
        self.closed = False

    async def search_movie(self, title, year=None):
        item = self.catalogue.get(('movie', title))
        return [dict(item)] if item else []

    async def search_tv(self, title, year=None):
        item = self.catalogue.get(('tv', title))
        return [dict(item)] if item else []

    def _apply_filters(self, item, media_type):
        return {'passed': not item.get('fail')}

    async def close(self):
        self.closed = True


def _llm_error():
    return LLMValidationError('bad json')


def _item(tmdb_id, title, year=2020, **extra):
    return {'id': tmdb_id, 'title': title, 'release_date': f'{year}-01-01', **extra}


def _suggestion(title, media_type='movie', pick_type='safe', year=2020):
    return {'title': title, 'year': year, 'media_type': media_type,
            'rationale': f'Because of {title}', 'pick_type': pick_type}


class SwipeServiceCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.db = _db()
        self.signals = FakeSignals()
        self.store = _PrefetchStore()
        self.service = SwipeService(config=dict(CONFIG), db=self.db, signals=self.signals,
                                    store=self.store)
        self.background = []
        patcher = patch.object(svc, '_run_in_background',
                               side_effect=lambda factory, name: self.background.append((name, factory)))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _drain(self):
        """Run the background jobs captured so far, as the worker thread would."""
        while self.background:
            _, factory = self.background.pop(0)
            try:
                await factory()
            except Exception:
                pass

    async def _next(self, *args, **kwargs):
        """Ask for a batch; if it is pending, let the background job finish and ask again."""
        result = await self.service.next_batch(*args, **kwargs)
        if not result['pending']:
            return result
        await self._drain()
        return await self.service.next_batch(*args, **kwargs)

    def _vote_many(self, user_id, count, vote='like'):
        for i in range(count):
            self.db.set_swipe_vote(user_id, 10_000 + i, 'movie', vote)

    def _patch_llm(self, suggestions, catalogue):
        tmdb = FakeTmdb(catalogue)
        llm = patch('api_service.services.llm.llm_service.generate_swipe_batch',
                    AsyncMock(return_value=suggestions))
        make = patch('api_service.services.ai_search.ai_search_service.AiSearchService._make_tmdb_client',
                     MagicMock(return_value=tmdb))
        return llm, make, tmdb


class TestContext(SwipeServiceCase):

    def test_linked_verified_profile_wins(self):
        self.db.media_profiles[2] = [
            {'provider': 'jellyfin', 'external_user_id': 'jf-guest', 'external_username': 'guest',
             'verified': 1},
            {'provider': 'plex', 'external_user_id': 'px', 'external_username': 'p', 'verified': 1},
        ]
        self.assertEqual(self.service.media_users(MEMBER), [{'id': 'jf-guest', 'name': 'guest'}])

    def test_unverified_link_is_ignored_and_member_gets_no_history(self):
        self.db.media_profiles[2] = [
            {'provider': 'jellyfin', 'external_user_id': 'jf-x', 'external_username': 'x', 'verified': 0},
        ]
        self.assertEqual(self.service.media_users(MEMBER), [])

    def test_admin_without_link_falls_back_to_selected_users(self):
        self.assertEqual(self.service.media_users(ADMIN), [{'id': 'jf-akira', 'name': 'akira'}])

    def test_language_uses_tmdb_language(self):
        self.assertEqual(self.service.language(1), 'fr')

    def test_status(self):
        self._vote_many(1, 3)
        status = self.service.status(ADMIN)
        self.assertTrue(status['llm_configured'])
        self.assertTrue(status['media_history'])
        self.assertEqual(status['streaming_region'], 'FR')
        self.assertEqual(status['calibration'], {'done': 3, 'target': CALIBRATION_TARGET,
                                                 'complete': False})
        self.assertFalse(status['profile_ready'])


class TestBatches(SwipeServiceCase):

    async def test_calibration_until_target_then_normal(self):
        suggestions = [_suggestion('Inception', pick_type='calibration')]
        catalogue = {('movie', 'Inception'): _item(27205, 'Inception', 2010)}
        llm, make, _ = self._patch_llm(suggestions, catalogue)
        with llm as generate, make:
            result = await self._next(ADMIN, media_type='movie')
        self.assertEqual(result['mode'], 'calibration')
        self.assertEqual(generate.await_args.kwargs['mode'], 'calibration')

        self._vote_many(1, CALIBRATION_TARGET)
        self.db.save_taste_profile(1, 'Loves sci-fi.')
        llm, make, _ = self._patch_llm([_suggestion('Arrival')],
                                       {('movie', 'Arrival'): _item(329865, 'Arrival', 2016)})
        with llm as generate, make:
            result = await self._next(ADMIN, media_type='movie')
        self.assertEqual(result['mode'], 'normal')
        kwargs = generate.await_args.kwargs
        self.assertEqual(kwargs['profile_text'], 'Loves sci-fi.')
        self.assertEqual(kwargs['language'], 'fr')
        self.assertEqual(kwargs['user_id'], 1)
        self.assertEqual(len(kwargs['recent_votes']), 15)

    async def test_normal_mode_without_profile_builds_it_first(self):
        self._vote_many(1, CALIBRATION_TARGET)
        llm, make, _ = self._patch_llm([], {})
        profile = patch('api_service.services.llm.llm_service.update_taste_profile',
                        AsyncMock(return_value='Fresh profile.'))
        with llm, make, profile as update:
            await self._next(ADMIN, media_type='movie')
        update.assert_awaited_once()
        self.assertEqual(self.db.get_taste_profile(1)['profile_text'], 'Fresh profile.')

    async def test_exclusions_filters_and_card_shape(self):
        self.db.save_taste_profile(1, 'Loves sci-fi.')
        self.db.set_swipe_vote(1, 1, 'movie', 'dislike')
        self.db.requested = {'2'}
        self.signals._library = {('3', 'movie')}
        self.signals._engagement = [{'title': 'Stargate Atlantis', 'media_type': 'tv'}]
        self.store.remember_served(1, [('4', 'movie')])
        suggestions = [
            _suggestion('Voted'), _suggestion('Requested'), _suggestion('Owned'),
            _suggestion('Served'), _suggestion('Stargate Atlantis', 'tv'),
            _suggestion('Unknown title'), _suggestion('Filtered'),
            _suggestion('Dune', pick_type='explore'), _suggestion('Dune'),
        ]
        catalogue = {
            ('movie', 'Voted'): _item(1, 'Voted'),
            ('movie', 'Requested'): _item(2, 'Requested'),
            ('movie', 'Owned'): _item(3, 'Owned'),
            ('movie', 'Served'): _item(4, 'Served'),
            ('tv', 'Stargate Atlantis'): {'id': 2290, 'name': 'Stargate Atlantis',
                                          'first_air_date': '2004-07-16'},
            ('movie', 'Filtered'): _item(6, 'Filtered', fail=True),
            ('movie', 'Dune'): _item(438631, 'Dune', 2021),
        }
        llm, make, tmdb = self._patch_llm(suggestions, catalogue)
        with llm, make:
            result = await self._next(ADMIN, media_type='both', mode='normal')

        self.assertEqual([c['id'] for c in result['cards']], [438631])
        card = result['cards'][0]
        self.assertEqual(card['media_type'], 'movie')
        self.assertEqual(card['year'], 2021)
        self.assertEqual(card['pick_type'], 'explore')
        self.assertEqual(card['rationale'], 'Because of Dune')
        self.assertIn('streaming', card)
        self.assertTrue(tmdb.closed)
        self.assertEqual(self.signals.engagement_users[-1], [{'id': 'jf-akira', 'name': 'akira'}])

    async def test_failed_first_profile_does_not_block_the_batch(self):
        self._vote_many(1, CALIBRATION_TARGET)
        llm, make, _ = self._patch_llm([_suggestion('Arrival')],
                                       {('movie', 'Arrival'): _item(329865, 'Arrival', 2016)})
        broken = patch('api_service.services.llm.llm_service.update_taste_profile',
                       AsyncMock(side_effect=_llm_error()))
        with llm as generate, make, broken:
            result = await self._next(ADMIN, media_type='movie')
        self.assertEqual([c['id'] for c in result['cards']], [329865])
        self.assertIsNone(generate.await_args.kwargs['profile_text'])

    async def test_media_type_filter_forces_kind(self):
        llm, make, _ = self._patch_llm(
            [_suggestion('Dark', media_type='movie')],
            {('tv', 'Dark'): {'id': 70523, 'name': 'Dark', 'first_air_date': '2017-12-01'}},
        )
        with llm, make:
            result = await self._next(ADMIN, media_type='tv', mode='normal')
        self.assertEqual([(c['id'], c['media_type']) for c in result['cards']], [(70523, 'tv')])

    async def test_invalid_arguments(self):
        with self.assertRaises(SwipeError):
            await self.service.next_batch(ADMIN, media_type='music')
        with self.assertRaises(SwipeError):
            await self.service.next_batch(ADMIN, mode='random')

    async def test_first_call_is_pending_and_starts_generation(self):
        llm, make, _ = self._patch_llm([], {})
        with llm as generate, make:
            result = await self.service.next_batch(ADMIN, media_type='movie', mode='calibration')
        self.assertTrue(result['pending'])
        self.assertEqual(result['cards'], [])
        generate.assert_not_awaited()
        self.assertEqual(len(self.background), 1)

    async def test_prefetched_batch_is_served_without_new_llm_call(self):
        catalogue = {('movie', f'M{i}'): _item(100 + i, f'M{i}') for i in range(4)}
        llm, make, _ = self._patch_llm([_suggestion('M0'), _suggestion('M1')], catalogue)
        with llm, make:
            first = await self._next(ADMIN, media_type='movie', mode='calibration')
        self.assertEqual([c['id'] for c in first['cards']], [100, 101])
        # Serving the first batch started the next one.
        self.assertEqual(len(self.background), 1)

        llm, make, _ = self._patch_llm([_suggestion('M2'), _suggestion('M3')], catalogue)
        with llm, make:
            await self._drain()
        # The user votes on one prefetched card before asking for the next batch.
        self.db.set_swipe_vote(1, 102, 'movie', 'like')
        llm, make, _ = self._patch_llm([], catalogue)
        with llm as generate, make:
            result = await self.service.next_batch(ADMIN, media_type='movie', mode='calibration')
        generate.assert_not_awaited()
        self.assertFalse(result['pending'])
        self.assertEqual([c['id'] for c in result['cards']], [103])

    async def test_empty_batch_is_not_pending_and_does_not_regenerate(self):
        llm, make, _ = self._patch_llm([_suggestion('Unknown')], {})
        with llm as generate, make:
            result = await self._next(ADMIN, media_type='movie', mode='calibration')
            self.assertFalse(result['pending'])
            self.assertEqual(result['cards'], [])
            self.assertEqual(self.background, [])
            self.assertEqual(generate.await_count, 1)
            # Asking again (the "Try again" button) starts a new attempt.
            self.assertTrue((await self.service.next_batch(ADMIN, media_type='movie',
                                                          mode='calibration'))['pending'])

    async def test_one_generation_at_a_time_per_key(self):
        llm, make, _ = self._patch_llm([], {})
        with llm, make:
            await self.service.next_batch(ADMIN, media_type='movie', mode='calibration')
            await self.service.next_batch(ADMIN, media_type='movie', mode='calibration')
        self.assertEqual(len(self.background), 1)

    async def test_background_failure_is_reported_once(self):
        make = patch('api_service.services.ai_search.ai_search_service.AiSearchService._make_tmdb_client',
                     MagicMock(return_value=FakeTmdb({})))
        broken = patch('api_service.services.llm.llm_service.generate_swipe_batch',
                       AsyncMock(side_effect=_llm_error()))
        with broken, make:
            self.assertTrue((await self.service.next_batch(ADMIN, mode='calibration'))['pending'])
            await self._drain()
            with self.assertRaises(LLMValidationError):
                await self.service.next_batch(ADMIN, mode='calibration')
            # The error is reported once; the next call starts a fresh attempt.
            self.assertTrue((await self.service.next_batch(ADMIN, mode='calibration'))['pending'])


class TestPreferencesAndWarmUp(SwipeServiceCase):

    async def test_served_batch_records_media_type_but_not_with_a_mood(self):
        catalogue = {('tv', 'Dark'): {'id': 70523, 'name': 'Dark', 'first_air_date': '2017-12-01'}}
        llm, make, _ = self._patch_llm([_suggestion('Dark', media_type='tv')], catalogue)
        with llm, make:
            await self._next(ADMIN, media_type='tv', mode='calibration', mood='sci-fi')
        self.assertEqual(self.db.get_swipe_active_users(), [])
        self.background.clear()
        self.store = self.service.store = _PrefetchStore()
        llm, make, _ = self._patch_llm([_suggestion('Dark', media_type='tv')], catalogue)
        with llm, make:
            await self._next(ADMIN, media_type='tv', mode='calibration')
        self.assertEqual(self.db.get_swipe_active_users(), [(1, 'tv')])

    def test_warm_up_prepares_active_users_once(self):
        self.db.set_swipe_preference(1, 'tv')
        self.db.set_swipe_preference(2, 'movie')
        self.assertEqual(self.service.warm_up(), 2)
        self.assertEqual(sorted(name for name, _ in self.background), ['batch-1', 'batch-2'])
        # Already being generated: nothing new is started.
        self.assertEqual(self.service.warm_up(), 0)

    async def test_warm_up_batch_is_served_for_the_default_filters(self):
        self.db.set_swipe_preference(1, 'movie')
        llm, make, _ = self._patch_llm([_suggestion('Arrival')],
                                       {('movie', 'Arrival'): _item(329865, 'Arrival', 2016)})
        with llm as generate, make:
            self.service.warm_up()
            await self._drain()
            result = await self.service.next_batch(ADMIN, media_type='movie')
        self.assertFalse(result['pending'])
        self.assertEqual([c['id'] for c in result['cards']], [329865])
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(generate.await_args.kwargs['mode'], 'calibration')

    def test_warm_up_skips_users_without_llm(self):
        self.db.set_swipe_preference(1, 'tv')
        self.service.config.pop('OPENAI_API_KEY')
        self.assertEqual(self.service.warm_up(), 0)


class TestLocalization(SwipeServiceCase):

    async def test_cards_are_localized_in_the_display_language(self):
        seen = {}

        def fake_localize(items, language, db, api_key, fields):
            seen.update(language=language, fields=fields)
            for item in items:
                item['title'], item['overview'] = 'Premier Contact', 'Résumé en français.'

        llm, make, _ = self._patch_llm([_suggestion('Arrival')],
                                       {('movie', 'Arrival'): _item(329865, 'Arrival', 2016)})
        with llm, make, patch('api_service.services.tmdb.localization.localize_items',
                              side_effect=fake_localize):
            result = await self._next(ADMIN, media_type='movie', mode='calibration')
        self.assertEqual(seen['language'], 'fr')
        self.assertEqual(seen['fields'], [('id', 'media_type', 'title', 'overview')])
        self.assertEqual(result['cards'][0]['title'], 'Premier Contact')

    async def test_english_needs_no_translation(self):
        self.service.config['TMDB_LANGUAGE'] = 'en'
        llm, make, _ = self._patch_llm([_suggestion('Arrival')],
                                       {('movie', 'Arrival'): _item(329865, 'Arrival', 2016)})
        with llm, make, patch('api_service.services.tmdb.localization.localize_items') as localize:
            await self._next(ADMIN, media_type='movie', mode='calibration')
        localize.assert_not_called()

    async def test_translation_failure_keeps_the_cards(self):
        llm, make, _ = self._patch_llm([_suggestion('Arrival')],
                                       {('movie', 'Arrival'): _item(329865, 'Arrival', 2016)})
        with llm, make, patch('api_service.services.tmdb.localization.localize_items',
                              side_effect=RuntimeError('TMDb down')):
            result = await self._next(ADMIN, media_type='movie', mode='calibration')
        self.assertEqual(result['cards'][0]['title'], 'Arrival')


class TestBalance(unittest.TestCase):

    def test_keeps_explore_share_and_spreads_it(self):
        cards = [{'id': i, 'pick_type': 'safe'} for i in range(10)]
        cards += [{'id': 100 + i, 'pick_type': 'explore'} for i in range(5)]
        batch = SwipeService._balance(cards, 10, 'normal')
        self.assertEqual(len(batch), 10)
        positions = [i for i, c in enumerate(batch) if c['pick_type'] == 'explore']
        self.assertEqual(len(positions), 3)
        self.assertNotEqual(positions, [7, 8, 9])
        self.assertEqual(len({c['id'] for c in batch}), 10)

    def test_short_batches_are_returned_as_is(self):
        cards = [{'id': 1, 'pick_type': 'explore'}]
        self.assertEqual(SwipeService._balance(cards, 10, 'normal'), cards)

    def test_fills_with_explore_when_safe_runs_out(self):
        cards = [{'id': 1, 'pick_type': 'safe'}] + [{'id': 10 + i, 'pick_type': 'explore'}
                                                    for i in range(12)]
        self.assertEqual(len(SwipeService._balance(cards, 10, 'normal')), 10)


class TestVotes(SwipeServiceCase):

    CARD = {'id': 438631, 'media_type': 'movie', 'title': 'Dune', 'release_date': '2021-09-15',
            'genres': ['Science Fiction'], 'rationale': 'Because of BR2049', 'pick_type': 'explore'}

    def test_vote_stores_card_details(self):
        result = self.service.vote(ADMIN, dict(self.CARD), 'like')
        self.assertFalse(result['profile_refresh'])
        [vote] = self.db.get_swipe_votes(1)
        self.assertEqual((vote['tmdb_id'], vote['vote'], vote['year'], vote['pick_type']),
                         ('438631', 'like', 2021, 'explore'))
        self.assertEqual(self.background, [])

    def test_profile_refresh_every_n_votes(self):
        self.db.save_taste_profile(1, 'Existing.')
        self._vote_many(1, CALIBRATION_TARGET + 5)
        for i in range(PROFILE_REFRESH_EVERY - 1):
            self.db.increment_taste_profile_votes(1)
        result = self.service.vote(ADMIN, dict(self.CARD), 'dislike')
        self.assertTrue(result['profile_refresh'])
        self.assertTrue(self.background[0][0].startswith('profile-'))

    def test_profile_refresh_when_calibration_completes(self):
        self._vote_many(1, CALIBRATION_TARGET - 1)
        result = self.service.vote(ADMIN, dict(self.CARD), 'seen_liked')
        self.assertTrue(result['profile_refresh'])

    def test_invalid_vote_and_card(self):
        for card, vote in ((dict(self.CARD), 'love'), ({'id': 'abc', 'media_type': 'movie'}, 'like'),
                           ({'id': 1, 'media_type': 'music'}, 'like'), (None, 'like')):
            with self.subTest(card=card, vote=vote), self.assertRaises(SwipeError):
                self.service.vote(ADMIN, card, vote)


class TestRequests(SwipeServiceCase):

    CARD = TestVotes.CARD

    def _patch_seer(self, enqueued=True, approval=True):
        seer = MagicMock()
        seer.request_media = AsyncMock(return_value=enqueued)
        seer.close = AsyncMock()
        cls = patch('api_service.services.seer.seer_client.SeerClient', MagicMock(return_value=seer))
        env = patch('api_service.config.config.load_env_vars',
                    return_value={'REQUIRE_REQUEST_APPROVAL': approval})
        return cls, env, seer

    async def test_request_goes_through_queue_with_owner_and_profiles(self):
        cls, env, seer = self._patch_seer()
        with cls as seer_cls, env:
            result = await self.service.request(ADMIN, dict(self.CARD))
        self.assertEqual(result, {'request_status': 'awaiting_approval'})
        kwargs = seer_cls.call_args.kwargs
        self.assertEqual(kwargs['queue_context'], {'owner_id': 1, 'delivery_mode': 'inherit'})
        self.assertEqual(kwargs['anime_profile_config'], {'default_movie': {'profileId': 9}})
        media_type, media = seer.request_media.await_args.args
        self.assertEqual(media_type, 'movie')
        self.assertEqual(media['id'], 438631)
        self.assertNotIn('pick_type', media)
        self.assertEqual(seer.request_media.await_args.kwargs['source'], {'id': 'swipe'})
        seer.close.assert_awaited()
        [vote] = self.db.get_swipe_votes(1)
        self.assertEqual(vote['vote'], 'like')
        self.assertTrue(vote['requested'])

    async def test_request_without_approval_is_queued(self):
        cls, env, _ = self._patch_seer(approval=False)
        with cls, env:
            self.assertEqual(await self.service.request(ADMIN, dict(self.CARD)), {'request_status': 'queued'})

    async def test_already_requested_keeps_existing_vote(self):
        self.service.vote(ADMIN, dict(self.CARD), 'seen_liked')
        cls, env, _ = self._patch_seer(enqueued=False)
        with cls, env:
            result = await self.service.request(ADMIN, dict(self.CARD))
        self.assertEqual(result, {'request_status': 'already_requested'})
        [vote] = self.db.get_swipe_votes(1)
        self.assertEqual(vote['vote'], 'seen_liked')
        self.assertFalse(vote['requested'])


class TestProfile(SwipeServiceCase):

    async def test_refresh_keeps_manual_edit_flag_and_sends_new_votes(self):
        self.db.save_taste_profile(1, 'No superheroes.', user_edited=True)
        self._vote_many(1, 3)
        for _ in range(3):
            self.db.increment_taste_profile_votes(1)
        with patch('api_service.services.llm.llm_service.update_taste_profile',
                   AsyncMock(return_value='No superheroes. Likes slow sci-fi.')) as update:
            text = await self.service.refresh_profile(ADMIN)
        self.assertEqual(text, 'No superheroes. Likes slow sci-fi.')
        kwargs = update.await_args.kwargs
        self.assertTrue(kwargs['user_edited'])
        self.assertEqual(kwargs['current_profile'], 'No superheroes.')
        self.assertEqual(len(kwargs['new_votes']), 3)
        profile = self.db.get_taste_profile(1)
        self.assertTrue(profile['user_edited'])
        self.assertEqual(profile['votes_since_update'], 0)

    async def test_nothing_to_build_from(self):
        self.signals._engagement = []
        with patch('api_service.services.llm.llm_service.update_taste_profile', AsyncMock()) as update:
            self.assertIsNone(await self.service.refresh_profile(MEMBER))
        update.assert_not_awaited()

    async def test_background_refresh_and_state(self):
        self._vote_many(1, 2)
        self.assertTrue(self.service.start_profile_refresh(ADMIN))
        self.assertFalse(self.service.start_profile_refresh(ADMIN))
        self.assertTrue(self.service.profile_state(ADMIN)['refreshing'])
        with patch('api_service.services.llm.llm_service.update_taste_profile',
                   AsyncMock(return_value='Written in the background.')):
            await self._drain()
        state = self.service.profile_state(ADMIN)
        self.assertFalse(state['refreshing'])
        self.assertIsNone(state['refresh_error'])
        self.assertEqual(state['profile']['profile_text'], 'Written in the background.')

    async def test_background_refresh_error_is_reported_once(self):
        self._vote_many(1, 2)
        self.service.start_profile_refresh(ADMIN)
        with patch('api_service.services.llm.llm_service.update_taste_profile',
                   AsyncMock(side_effect=_llm_error())):
            await self._drain()
        self.assertEqual(self.service.profile_state(ADMIN)['refresh_error'], 'bad json')
        self.assertIsNone(self.service.profile_state(ADMIN)['refresh_error'])

    def test_manual_save_and_reset(self):
        profile = self.service.save_profile(ADMIN, 'Mine.')
        self.assertTrue(profile['user_edited'])
        with self.assertRaises(SwipeError):
            self.service.save_profile(ADMIN, '  ')
        self._vote_many(1, 2)
        self.store.remember_served(1, [('5', 'movie')])
        self.assertEqual(self.service.reset(ADMIN), {'deleted': 2})
        self.assertEqual(self.store.served(1), set())
        self.assertEqual(self.db.get_taste_profile(1)['profile_text'], 'Mine.')
