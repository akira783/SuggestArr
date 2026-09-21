"""Tests for the Swipe signal sources.

Covers:
- engagement classification and ranking (pure functions)
- JellyfinClient.get_engagement_items(): aggregation of titles, played episodes and
  the resume list; untouched titles skipped; failing queries degrade to []
- TMDbClient.get_streaming_availability() and get_imdb_id()
- OmdbClient.get_ratings()
- SwipeSignals: sources skipped when unconfigured, failures swallowed
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from api_service.services.swipe import engagement as eng
from api_service.services.swipe.signals import SwipeSignals
from api_service.services.jellyfin.jellyfin_client import JellyfinClient
from api_service.services.omdb.omdb_client import OmdbClient
from api_service.services.tmdb.tmdb_client import TMDbClient

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _days_ago(days):
    return (NOW - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.1234567Z')


def _mock_response(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _tmdb_client():
    return TMDbClient(
        api_key='key', search_size=20, tmdb_threshold=0, tmdb_min_votes=0,
        include_no_ratings=True, filter_release_year=0, filter_language=[],
        filter_genre=[], filter_region_provider=None, filter_streaming_services=None,
    )


# ---------------------------------------------------------------------------
# Engagement classification
# ---------------------------------------------------------------------------

class TestClassifyEngagement(unittest.TestCase):

    def _tv(self, watched, total, days):
        return {'media_type': 'tv', 'episodes_watched': watched, 'episodes_total': total,
                'last_played': _days_ago(days) if days is not None else None}

    def _movie(self, **kwargs):
        item = {'media_type': 'movie', 'play_count': 0, 'played': False, 'progress_pct': None,
                'last_played': None}
        item.update(kwargs)
        return item

    def test_series_labels(self):
        cases = [
            (self._tv(10, 10, 400), eng.COMPLETED),
            (self._tv(3, 20, 5), eng.IN_PROGRESS),
            (self._tv(3, 20, 200), eng.ABANDONED),
            (self._tv(12, 20, 200), eng.PARTIALLY_WATCHED),
            # Long paused but heavily invested: a fan taking a break, not a rejection.
            (self._tv(35, 100, 90), eng.PARTIALLY_WATCHED),
            # Unknown total: a couple of episodes long ago means dropped.
            (self._tv(2, None, 200), eng.ABANDONED),
            (self._tv(8, None, 200), eng.PARTIALLY_WATCHED),
            # Between "recent" and "stale": not enough evidence to call it abandoned.
            (self._tv(1, 20, 45), eng.PARTIALLY_WATCHED),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(eng.classify_engagement(item, NOW), expected)

    def test_movie_labels(self):
        cases = [
            (self._movie(rewatched=True, played=True), eng.REWATCHED),
            # Remote playback inflates PlayCount: it is not a re-watch signal.
            (self._movie(play_count=9, played=True), eng.WATCHED),
            (self._movie(play_count=1, played=True), eng.WATCHED),
            (self._movie(progress_pct=95, last_played=_days_ago(100)), eng.WATCHED),
            (self._movie(progress_pct=40, last_played=_days_ago(2)), eng.IN_PROGRESS),
            (self._movie(progress_pct=40, last_played=_days_ago(90)), eng.ABANDONED),
            (self._movie(progress_pct=40), eng.PARTIALLY_WATCHED),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(eng.classify_engagement(item, NOW), expected)

    def test_parses_seven_digit_fractions_and_plain_dates(self):
        self.assertIsNotNone(eng._parse_date('2025-06-23T21:14:05.1234567Z'))
        self.assertIsNotNone(eng._parse_date('2025-06-23T21:14:05Z'))
        self.assertIsNone(eng._parse_date('not a date'))
        self.assertIsNone(eng._parse_date(None))


class TestSummarizeEngagement(unittest.TestCase):

    def test_ranks_by_effort_then_recency_and_describes_evidence(self):
        items = [
            {'title': 'Stargate Atlantis', 'media_type': 'tv', 'episodes_watched': 35,
             'episodes_total': 100, 'last_played': _days_ago(90), 'genres': ['Sci-Fi']},
            {'title': 'Blade Runner 2049', 'media_type': 'movie', 'rewatched': True, 'played': True,
             'last_played': _days_ago(120)},
            {'title': 'Black Panther', 'media_type': 'movie', 'play_count': 0, 'played': False,
             'progress_pct': 52, 'last_played': _days_ago(1)},
            {'title': None, 'media_type': 'movie', 'play_count': 9},
        ]

        summary = eng.summarize_engagement(items, now=NOW)

        self.assertEqual([s['title'] for s in summary],
                         ['Stargate Atlantis', 'Blade Runner 2049', 'Black Panther'])
        self.assertEqual(summary[0]['engagement'], eng.PARTIALLY_WATCHED)
        self.assertEqual(summary[0]['detail'], '35/100 episodes')
        self.assertEqual(summary[1]['engagement'], eng.REWATCHED)
        self.assertEqual(summary[1]['detail'], 'watched several times')
        self.assertEqual(summary[2]['engagement'], eng.IN_PROGRESS)
        self.assertEqual(summary[2]['detail'], 'stopped at 52%')

    def test_limit(self):
        items = [{'title': f'T{i}', 'media_type': 'movie', 'play_count': 1, 'played': True}
                 for i in range(10)]
        self.assertEqual(len(eng.summarize_engagement(items, limit=3, now=NOW)), 3)


# ---------------------------------------------------------------------------
# JellyfinClient.get_engagement_items
# ---------------------------------------------------------------------------

class TestJellyfinEngagement(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.client = JellyfinClient('http://jellyfin.local', 'token',
                                     library_ids=[{'id': 'lib1', 'name': 'Séries'}])
        self.titles = [
            {'Id': 's1', 'Type': 'Series', 'Name': 'Stargate Atlantis', 'ProductionYear': 2004,
             'ProviderIds': {'Tmdb': '2290'}, 'Genres': ['Sci-Fi'],
             'UserData': {'UnplayedItemCount': 65, 'PlayCount': 0, 'Played': False}},
            {'Id': 'm1', 'Type': 'Movie', 'Name': 'Blade Runner 2049', 'ProductionYear': 2017,
             'ProviderIds': {'Tmdb': '335984'},
             'UserData': {'PlayCount': 5, 'Played': True,
                          'LastPlayedDate': '2025-05-15T20:00:00.0000000Z'}},
            {'Id': 'm2', 'Type': 'Movie', 'Name': 'Top Gun: Maverick',
             'UserData': {'PlayCount': 0, 'Played': False, 'PlayedPercentage': 64.4,
                          'LastPlayedDate': '2026-09-17T20:00:00.0000000Z'}},
            {'Id': 'm3', 'Type': 'Movie', 'Name': 'Inception',
             'UserData': {'PlayCount': 0, 'Played': False}},
            {'Id': 's2', 'Type': 'Series', 'Name': 'Mercredi',
             'UserData': {'UnplayedItemCount': 8, 'PlayCount': 0, 'Played': False}},
        ]
        self.episodes = [
            {'SeriesId': 's1', 'UserData': {'LastPlayedDate': '2025-06-20T20:00:00.0000000Z'}},
            {'SeriesId': 's1', 'UserData': {'LastPlayedDate': '2025-06-23T21:00:00.0000000Z'}},
            {'SeriesId': None, 'UserData': {}},
        ]
        self.resume = [
            {'Type': 'Episode', 'SeriesId': 's2',
             'UserData': {'PlayedPercentage': 88.2, 'LastPlayedDate': '2025-08-12T20:00:00.0000000Z'}},
        ]

    def _session(self):
        def get(url, headers=None, params=None, timeout=None):
            if url.endswith('/Resume'):
                return _mock_response(200, {'Items': self.resume})
            if params.get('IncludeItemTypes') == 'Episode':
                return _mock_response(200, {'Items': self.episodes})
            return _mock_response(200, {'Items': self.titles})
        session = MagicMock()
        session.get = MagicMock(side_effect=get)
        return session

    async def test_aggregates_titles_episodes_and_resume(self):
        session = self._session()
        with patch.object(self.client, '_get_session', AsyncMock(return_value=session)):
            items = await self.client.get_engagement_items({'id': 'u1', 'name': 'akira'})

        by_title = {item['title']: item for item in items}
        self.assertEqual(set(by_title), {'Stargate Atlantis', 'Blade Runner 2049',
                                         'Top Gun: Maverick', 'Mercredi'})
        stargate = by_title['Stargate Atlantis']
        self.assertEqual(stargate['media_type'], 'tv')
        self.assertEqual(stargate['episodes_watched'], 2)
        self.assertEqual(stargate['episodes_total'], 67)
        self.assertEqual(stargate['last_played'], '2025-06-23T21:00:00.0000000Z')
        self.assertEqual(stargate['tmdb_id'], '2290')
        self.assertEqual(stargate['genres'], ['Sci-Fi'])
        self.assertEqual(by_title['Blade Runner 2049']['play_count'], 5)
        self.assertEqual(by_title['Top Gun: Maverick']['progress_pct'], 64)
        self.assertTrue(by_title['Mercredi']['in_progress'])
        self.assertEqual(by_title['Mercredi']['progress_pct'], 88)
        self.assertEqual(by_title['Mercredi']['episodes_watched'], 0)

    async def test_queries_are_user_scoped_and_library_filtered(self):
        session = self._session()
        with patch.object(self.client, '_get_session', AsyncMock(return_value=session)):
            await self.client.get_engagement_items({'id': 'u1', 'name': 'akira'})

        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertTrue(all(url.startswith('http://jellyfin.local/Users/u1/Items') for url in urls))
        library_calls = [call for call in session.get.call_args_list
                         if not call.args[0].endswith('/Resume')]
        self.assertTrue(all(call.kwargs['params']['ParentId'] == 'lib1' for call in library_calls))
        self.assertTrue(all(call.kwargs['params']['EnableUserData'] == 'true'
                            for call in session.get.call_args_list))

    async def test_failing_queries_degrade_to_empty(self):
        session = MagicMock()
        session.get = MagicMock(side_effect=aiohttp.ClientError('refused'))
        with patch.object(self.client, '_get_session', AsyncMock(return_value=session)):
            items = await self.client.get_engagement_items({'id': 'u1', 'name': 'akira'})
        self.assertEqual(items, [])

    async def test_http_error_degrades_to_empty(self):
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(401))
        with patch.object(self.client, '_get_session', AsyncMock(return_value=session)):
            self.assertEqual(await self.client.get_engagement_items({'id': 'u1'}), [])


# ---------------------------------------------------------------------------
# TMDb streaming availability / IMDb id
# ---------------------------------------------------------------------------

class TestTmdbStreaming(unittest.IsolatedAsyncioTestCase):

    PAYLOAD = {'results': {'FR': {
        'link': 'https://www.themoviedb.org/movie/1/watch?locale=FR',
        'flatrate': [
            {'provider_id': 337, 'provider_name': 'Disney Plus', 'logo_path': '/d.jpg',
             'display_priority': 3},
            {'provider_id': 8, 'provider_name': 'Netflix', 'logo_path': '/n.jpg',
             'display_priority': 1},
        ],
        'rent': [{'provider_id': 2, 'provider_name': 'Apple TV'}],
    }}}

    async def test_lists_flatrate_providers_in_display_order(self):
        client = _tmdb_client()
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(200, self.PAYLOAD))
        with patch.object(client, '_get_session', AsyncMock(return_value=session)):
            result = await client.get_streaming_availability(1, 'movie', 'fr')

        self.assertEqual(result['region'], 'FR')
        self.assertEqual([p['name'] for p in result['providers']], ['Netflix', 'Disney Plus'])
        self.assertEqual(result['providers'][0]['logo_path'], 'https://image.tmdb.org/t/p/w92/n.jpg')
        self.assertEqual(result['link'], 'https://www.themoviedb.org/movie/1/watch?locale=FR')

    async def test_none_when_not_streamed_in_region_or_no_region(self):
        client = _tmdb_client()
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(200, self.PAYLOAD))
        with patch.object(client, '_get_session', AsyncMock(return_value=session)):
            self.assertIsNone(await client.get_streaming_availability(1, 'movie', 'US'))
            self.assertIsNone(await client.get_streaming_availability(1, 'movie', None))
        self.assertEqual(session.get.call_count, 1)

    async def test_none_on_http_or_network_error(self):
        client = _tmdb_client()
        for side_effect in (None, aiohttp.ClientError('boom')):
            session = MagicMock()
            if side_effect:
                session.get = MagicMock(side_effect=side_effect)
            else:
                session.get = MagicMock(return_value=_mock_response(500))
            with patch.object(client, '_get_session', AsyncMock(return_value=session)):
                self.assertIsNone(await client.get_streaming_availability(1, 'movie', 'FR'))

    async def test_get_imdb_id_uses_external_ids_for_tv(self):
        client = _tmdb_client()
        with patch.object(client, '_get_tv_imdb_id', AsyncMock(return_value='tt1')) as tv, \
                patch.object(client, '_get_item_details',
                             AsyncMock(return_value={'imdb_id': 'tt2'})) as details:
            self.assertEqual(await client.get_imdb_id(5, 'tv'), 'tt1')
            self.assertEqual(await client.get_imdb_id(5, 'movie'), 'tt2')
        tv.assert_awaited_once_with(5)
        details.assert_awaited_once_with(5, 'movie')


# ---------------------------------------------------------------------------
# OMDb ratings
# ---------------------------------------------------------------------------

class TestOmdbRatings(unittest.IsolatedAsyncioTestCase):

    async def _ratings(self, payload, status=200):
        client = OmdbClient('key')
        session = MagicMock()
        session.get = MagicMock(return_value=_mock_response(status, payload))
        with patch.object(client, '_get_session', AsyncMock(return_value=session)):
            return await client.get_ratings('tt1856101')

    async def test_parses_all_sources(self):
        result = await self._ratings({
            'Response': 'True', 'imdbRating': '8.0', 'imdbVotes': '712,345', 'Metascore': '81',
            'Ratings': [{'Source': 'Internet Movie Database', 'Value': '8.0/10'},
                        {'Source': 'Rotten Tomatoes', 'Value': '88%'}],
        })
        self.assertEqual(result, {'imdb_rating': 8.0, 'imdb_votes': 712345,
                                  'rotten_tomatoes': 88, 'metascore': 81})

    async def test_missing_values_become_none(self):
        result = await self._ratings({'Response': 'True', 'imdbRating': 'N/A',
                                      'imdbVotes': 'N/A', 'Metascore': 'N/A'})
        self.assertEqual(result, {'imdb_rating': None, 'imdb_votes': None,
                                  'rotten_tomatoes': None, 'metascore': None})

    async def test_not_found_and_errors(self):
        self.assertIsNone(await self._ratings({'Response': 'False', 'Error': 'Not found'}))
        self.assertIsNone(await self._ratings({}, status=401))
        self.assertIsNone(await OmdbClient('key').get_ratings(None))


# ---------------------------------------------------------------------------
# SwipeSignals facade
# ---------------------------------------------------------------------------

class TestSwipeSignals(unittest.IsolatedAsyncioTestCase):

    BASE = {'SELECTED_SERVICE': 'jellyfin', 'JELLYFIN_API_URL': 'http://jf', 'JELLYFIN_TOKEN': 't',
            'TMDB_API_KEY': 'k'}

    async def test_engagement_skipped_without_media_server_or_linked_user(self):
        for config, users in (({**self.BASE, 'SELECTED_SERVICE': 'plex'}, ['u1']),
                              ({**self.BASE, 'JELLYFIN_TOKEN': ''}, ['u1']),
                              (self.BASE, [])):
            with patch.object(JellyfinClient, 'get_engagement_items', AsyncMock()) as fetch:
                self.assertEqual(await SwipeSignals(config).engagement(users), [])
                fetch.assert_not_awaited()

    async def test_engagement_summarises_each_linked_user(self):
        raw = [{'title': 'Dark', 'media_type': 'tv', 'episodes_watched': 26, 'episodes_total': 26}]
        with patch.object(JellyfinClient, 'get_engagement_items',
                          AsyncMock(return_value=raw)) as fetch:
            summary = await SwipeSignals(self.BASE).engagement(['u1'])
        fetch.assert_awaited_once_with({'id': 'u1', 'name': 'u1'})
        self.assertEqual(summary[0]['title'], 'Dark')
        self.assertEqual(summary[0]['engagement'], eng.COMPLETED)

    async def test_engagement_failure_is_swallowed(self):
        with patch.object(JellyfinClient, 'get_engagement_items',
                          AsyncMock(side_effect=RuntimeError('down'))):
            self.assertEqual(await SwipeSignals(self.BASE).engagement(['u1']), [])

    async def test_enrich_cards_adds_badge_and_ratings(self):
        config = {**self.BASE, 'FILTER_REGION_PROVIDER': 'fr', 'OMDB_API_KEY': 'o',
                  'FILTER_STREAMING_SERVICES': [{'provider_id': 8, 'provider_name': 'Netflix'}]}
        streaming = {'region': 'FR', 'providers': [{'id': 8, 'name': 'Netflix', 'logo_path': None}],
                     'link': None}
        cards = [{'id': 1, 'media_type': 'movie'}, {'id': 2, 'media_type': 'tv'}]
        with patch.object(TMDbClient, 'get_streaming_availability',
                          AsyncMock(side_effect=[dict(streaming), None])) as providers, \
                patch.object(TMDbClient, 'get_imdb_id', AsyncMock(return_value='tt1')), \
                patch.object(OmdbClient, 'get_ratings',
                             AsyncMock(return_value={'imdb_rating': 8.0})):
            result = await SwipeSignals(config).enrich_cards(cards)

        self.assertIs(result, cards)
        self.assertEqual(providers.await_args_list[0].args, (1, 'movie', 'FR'))
        self.assertTrue(cards[0]['streaming']['on_user_services'])
        self.assertIsNone(cards[1]['streaming'])
        self.assertEqual(cards[0]['ratings'], {'imdb_rating': 8.0})

    async def test_enrich_cards_without_region_or_omdb_touches_nothing(self):
        cards = [{'id': 1, 'media_type': 'movie'}]
        with patch.object(TMDbClient, 'get_streaming_availability', AsyncMock()) as providers:
            await SwipeSignals(self.BASE).enrich_cards(cards)
        providers.assert_not_awaited()
        self.assertEqual(cards[0], {'id': 1, 'media_type': 'movie', 'streaming': None,
                                    'ratings': None})

    async def test_enrich_cards_failure_on_one_card_keeps_the_others(self):
        config = {**self.BASE, 'FILTER_REGION_PROVIDER': 'FR'}
        ok = {'region': 'FR', 'providers': [{'id': 8, 'name': 'Netflix', 'logo_path': None}],
              'link': None}
        cards = [{'id': 1, 'media_type': 'movie'}, {'id': 2, 'media_type': 'movie'}]

        async def availability(content_id, media_type, region):
            if content_id == 1:
                raise RuntimeError('TMDb down')
            return dict(ok)

        with patch.object(TMDbClient, 'get_streaming_availability', side_effect=availability):
            await SwipeSignals(config).enrich_cards(cards)
        self.assertIsNone(cards[0]['streaming'])
        self.assertEqual(cards[1]['streaming']['providers'][0]['name'], 'Netflix')
        self.assertFalse(cards[1]['streaming']['on_user_services'])
