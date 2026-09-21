"""Tests for the Swipe blueprint: caller scoping and error mapping."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from flask import Flask, g

from api_service.blueprints.swipe import routes
from api_service.exceptions.api_exceptions import LLMNotConfiguredError, LLMValidationError
from api_service.services.swipe.swipe_service import SwipeError

USER = {'id': '7', 'role': 'user', 'username': 'akira'}


class SwipeRouteCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.app = Flask(__name__)
        self.service = MagicMock()
        self.service.llm_configured.return_value = True
        patcher = patch.object(routes, 'SwipeService', return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _context(self, **kwargs):
        context = self.app.test_request_context(**kwargs)
        context.push()
        self.addCleanup(context.pop)
        g.current_user = USER


class TestBatch(SwipeRouteCase):

    async def test_passes_query_and_caller(self):
        self.service.next_batch = AsyncMock(return_value={'mode': 'normal', 'cards': [],
                                                          'calibration': {'done': 15, 'target': 15}})
        self._context(query_string={'media_type': 'tv', 'mood': 'sci-fi', 'mode': 'auto'})

        response, status = await routes.swipe_batch.__wrapped__()

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()['mode'], 'normal')
        self.service.next_batch.assert_awaited_once_with(USER, media_type='tv', mood='sci-fi',
                                                         mode='auto')

    async def test_llm_not_configured_is_checked_before_generating(self):
        self.service.llm_configured.return_value = False
        self.service.next_batch = AsyncMock()
        self._context()

        response, status = await routes.swipe_batch.__wrapped__()

        self.assertEqual(status, 400)
        self.assertEqual(response.get_json()['code'], 'llm_not_configured')
        self.service.next_batch.assert_not_awaited()

    async def test_error_mapping(self):
        cases = [
            (SwipeError('media_type must be one of movie, tv, both'), 400, 'invalid_input'),
            (LLMNotConfiguredError('no llm'), 400, 'llm_not_configured'),
            (LLMValidationError('bad json'), 502, 'llm_invalid'),
            (RuntimeError('boom'), 500, None),
        ]
        for exc, expected_status, code in cases:
            with self.subTest(exc=exc):
                self.service.next_batch = AsyncMock(side_effect=exc)
                self._context()
                response, status = await routes.swipe_batch.__wrapped__()
                self.assertEqual(status, expected_status)
                self.assertEqual(response.get_json().get('code'), code)
                self.assertNotIn('boom', response.get_json()['message'])


class TestVoteAndRequest(SwipeRouteCase):

    def test_vote(self):
        self.service.vote.return_value = {'vote': {'vote': 'like'}, 'profile_refresh': False}
        card = {'id': 1, 'media_type': 'movie'}
        self._context(method='POST', json={'card': card, 'vote': 'like'})

        response, status = routes.swipe_vote.__wrapped__()

        self.assertEqual(status, 200)
        self.service.vote.assert_called_once_with(USER, card, 'like')

    def test_vote_invalid_input(self):
        self.service.vote.side_effect = SwipeError('vote must be one of ...')
        self._context(method='POST', json={'card': None, 'vote': 'love'})
        response, status = routes.swipe_vote.__wrapped__()
        self.assertEqual(status, 400)

    async def test_request(self):
        self.service.request = AsyncMock(return_value={'request_status': 'awaiting_approval'})
        card = {'id': 1, 'media_type': 'movie'}
        self._context(method='POST', json={'card': card})

        response, status = await routes.swipe_request.__wrapped__()

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()['status'], 'success')
        self.assertEqual(response.get_json()['request_status'], 'awaiting_approval')
        self.service.request.assert_awaited_once_with(USER, card)


class TestProfileRoutes(SwipeRouteCase):

    def test_get_profile_for_caller(self):
        self.service.profile_state.return_value = {'profile': None, 'refreshing': True,
                                                   'refresh_error': None}
        self._context()
        response, status = routes.swipe_profile_get()
        self.assertEqual(status, 200)
        self.assertIsNone(response.get_json()['profile'])
        self.assertTrue(response.get_json()['refreshing'])
        self.service.profile_state.assert_called_once_with(USER)

    def test_put_profile_requires_string(self):
        self._context(method='PUT', json={'profile_text': 42})
        response, status = routes.swipe_profile_put.__wrapped__()
        self.assertEqual(status, 400)
        self.service.save_profile.assert_not_called()

    def test_put_profile(self):
        self.service.save_profile.return_value = {'profile_text': 'Mine.', 'user_edited': True}
        self._context(method='PUT', json={'profile_text': 'Mine.'})
        response, status = routes.swipe_profile_put.__wrapped__()
        self.assertEqual(status, 200)
        self.service.save_profile.assert_called_once_with(USER, 'Mine.')

    def test_refresh_starts_in_background(self):
        self.service.start_profile_refresh.return_value = True
        self._context(method='POST')
        response, status = routes.swipe_profile_refresh.__wrapped__()
        self.assertEqual(status, 202)
        self.assertEqual(response.get_json()['started'], True)
        self.service.start_profile_refresh.assert_called_once_with(USER)

    def test_refresh_needs_llm(self):
        self.service.llm_configured.return_value = False
        self._context(method='POST')
        response, status = routes.swipe_profile_refresh.__wrapped__()
        self.assertEqual(status, 400)
        self.service.start_profile_refresh.assert_not_called()

    def test_stats_and_reset(self):
        self.service.db.get_swipe_stats.return_value = {'total': 0}
        self.service.reset.return_value = {'deleted': 3}
        self._context()
        self.assertEqual(routes.swipe_stats()[0].get_json()['stats'], {'total': 0})
        self.service.db.get_swipe_stats.assert_called_once_with(7)
        self.assertEqual(routes.swipe_votes_reset.__wrapped__()[0].get_json()['deleted'], 3)

    def test_blueprint_exposes_all_routes(self):
        app = Flask(__name__)
        app.register_blueprint(routes.swipe_bp, url_prefix='/api/swipe')
        rules = {(rule.rule, method) for rule in app.url_map.iter_rules()
                 for method in rule.methods - {'HEAD', 'OPTIONS'}}
        self.assertEqual(
            {r for r in rules if r[0].startswith('/api/swipe')},
            {('/api/swipe/status', 'GET'), ('/api/swipe/batch', 'GET'),
             ('/api/swipe/vote', 'POST'), ('/api/swipe/request', 'POST'),
             ('/api/swipe/profile', 'GET'), ('/api/swipe/profile', 'PUT'),
             ('/api/swipe/profile/refresh', 'POST'), ('/api/swipe/stats', 'GET'),
             ('/api/swipe/votes', 'DELETE')},
        )
