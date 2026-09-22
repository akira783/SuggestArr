"""Tests for the Swipe prompts and LLM calls (llm_service)."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from api_service.exceptions.api_exceptions import LLMNotConfiguredError
from api_service.services.llm import llm_service
from api_service.services.llm.llm_service import (
    SWIPE_PROFILE_MAX_CHARS,
    SWIPE_PROFILE_TARGET_CHARS,
    build_swipe_batch_prompt,
    build_taste_profile_prompt,
    generate_swipe_batch,
    update_taste_profile,
)

ENGAGEMENT = [
    {'title': 'Stargate Atlantis', 'year': 2004, 'media_type': 'tv',
     'engagement': 'partially_watched', 'detail': '35/99 episodes',
     'genres': ['Science Fiction', 'Action', 'Adventure', 'Drama']},
    {'title': 'Andor', 'year': 2022, 'media_type': 'tv', 'engagement': 'abandoned',
     'detail': '3/24 episodes'},
]
VOTES = [
    {'title': 'Dark', 'year': 2017, 'media_type': 'tv', 'vote': 'like', 'pick_type': 'explore'},
    {'title': 'The Marvels', 'year': 2023, 'media_type': 'movie', 'vote': 'dislike',
     'pick_type': 'safe'},
    {'title': 'Inception', 'year': 2010, 'media_type': 'movie', 'vote': 'seen_liked',
     'pick_type': 'calibration'},
]


class TestSwipeBatchPrompt(unittest.TestCase):

    def test_normal_mode_mixes_safe_and_explore_and_lists_evidence(self):
        prompt = build_swipe_batch_prompt(
            count=10, media_type='tv', mode='normal', explore_ratio=0.3,
            profile_text='Loves thoughtful sci-fi.', engagement=ENGAGEMENT, recent_votes=VOTES,
            exclude_titles=[{'title': 'Silo', 'year': 2023, 'media_type': 'tv'}],
            mood='sci-fi tonight', language='fr',
        )
        self.assertIn('Pick 10 TV shows', prompt)
        self.assertIn('about 7 SAFE picks', prompt)
        self.assertIn('about 3 ADVENTUROUS picks', prompt)
        self.assertIn('Loves thoughtful sci-fi.', prompt)
        self.assertIn('Stargate Atlantis (TV, 2004) [Science Fiction, Action, Adventure]: '
                      'watched part of it, paused (35/99 episodes)', prompt)
        self.assertIn('a single film watched once is weak', prompt)
        self.assertIn('Andor (TV, 2022): started, then dropped (3/24 episodes)', prompt)
        self.assertIn('Dark (TV, 2017): LIKED [adventurous pick]', prompt)
        self.assertIn('The Marvels (movie, 2023): DISLIKED', prompt)
        self.assertIn('Inception (movie, 2010): already seen, LIKED it', prompt)
        self.assertIn('Silo (TV, 2023)', prompt)
        self.assertIn('"sci-fi tonight"', prompt)
        self.assertIn('ISO code "fr"', prompt)
        self.assertNotIn('VERY well-known', prompt)

    def test_calibration_mode_asks_for_well_known_contrasting_titles(self):
        prompt = build_swipe_batch_prompt(count=15, media_type='both', mode='calibration')
        self.assertIn('VERY well-known movies and TV shows (roughly half of each)', prompt)
        self.assertIn('contrasting', prompt)
        self.assertIn('"calibration" for every card', prompt)
        self.assertNotIn('ADVENTUROUS', prompt)
        self.assertIn('No history yet.', prompt)

    def test_small_batches_still_get_one_explore_pick(self):
        prompt = build_swipe_batch_prompt(count=2, media_type='movie', mode='normal')
        self.assertIn('about 1 SAFE picks', prompt)
        self.assertIn('about 1 ADVENTUROUS picks', prompt)


class TestTasteProfilePrompt(unittest.TestCase):

    def test_first_profile(self):
        prompt = build_taste_profile_prompt(current_profile=None, user_edited=False,
                                            engagement=ENGAGEMENT, language='fr')
        self.assertTrue(prompt.startswith('Write a concise taste profile'))
        self.assertIn('Stargate Atlantis', prompt)
        self.assertIn(f'at most {SWIPE_PROFILE_TARGET_CHARS} characters', prompt)
        self.assertIn('Loves / Avoids / Nuances', prompt)
        self.assertIn('ISO code "fr"', prompt)

    def test_user_edited_profile_is_ground_truth(self):
        prompt = build_taste_profile_prompt(current_profile='No superheroes.', user_edited=True,
                                            new_votes=VOTES)
        self.assertTrue(prompt.startswith('Revise'))
        self.assertIn('every statement below is ground truth', prompt)
        self.assertIn('never contradict', prompt)
        self.assertIn('Dark (TV, 2017): LIKED [adventurous pick]', prompt)

    def test_generated_profile_has_no_ground_truth_guard(self):
        prompt = build_taste_profile_prompt(current_profile='Likes sci-fi.', user_edited=False)
        self.assertNotIn('ground truth', prompt)


def _fake_client(payload):
    client = MagicMock()
    message = MagicMock()
    message.content = json.dumps(payload)
    response = MagicMock()
    response.choices = [MagicMock(message=message)]
    client.chat.completions.create = AsyncMock(return_value=response)
    client.close = AsyncMock()
    return client


class TestSwipeLlmCalls(unittest.IsolatedAsyncioTestCase):

    CONFIG = {'LLM_MODEL': 'gpt-4o-mini', 'LLM_MAX_RETRIES': 0}

    async def test_generate_swipe_batch_returns_validated_cards(self):
        client = _fake_client({'cards': [
            {'title': 'Dune', 'year': 2021, 'media_type': 'movie',
             'rationale': 'Tu as adoré Blade Runner 2049.', 'pick_type': 'safe'},
        ]})
        with patch.object(llm_service, 'get_llm_client', return_value=client) as get_client, \
                patch.object(llm_service.ConfigService, 'get_runtime_config', return_value=self.CONFIG):
            cards = await generate_swipe_batch(user_id=7, count=1, media_type='movie', mode='normal')

        get_client.assert_called_once_with(7)
        self.assertEqual(cards, [{'title': 'Dune', 'year': 2021, 'media_type': 'movie',
                                  'rationale': 'Tu as adoré Blade Runner 2049.', 'pick_type': 'safe'}])
        client.close.assert_awaited()

    async def test_invalid_pick_type_is_rejected(self):
        client = _fake_client({'cards': [
            {'title': 'Dune', 'year': 2021, 'media_type': 'movie', 'rationale': 'x',
             'pick_type': 'random'},
        ]})
        with patch.object(llm_service, 'get_llm_client', return_value=client), \
                patch.object(llm_service.ConfigService, 'get_runtime_config', return_value=self.CONFIG):
            with self.assertRaises(llm_service.LLMValidationError):
                await generate_swipe_batch(user_id=7, count=1, media_type='movie', mode='normal')

    async def test_update_taste_profile_trims_to_max_length(self):
        client = _fake_client({'profile_text': '  ' + 'x' * (SWIPE_PROFILE_MAX_CHARS + 50)})
        with patch.object(llm_service, 'get_llm_client', return_value=client), \
                patch.object(llm_service.ConfigService, 'get_runtime_config', return_value=self.CONFIG):
            text = await update_taste_profile(user_id=7, current_profile=None, user_edited=False)
        self.assertEqual(len(text), SWIPE_PROFILE_MAX_CHARS)

    async def test_not_configured(self):
        with patch.object(llm_service, 'get_llm_client', return_value=None):
            with self.assertRaises(LLMNotConfiguredError):
                await generate_swipe_batch(user_id=7, count=1, media_type='movie', mode='normal')


class TestNoveltyAndRecalibration(unittest.TestCase):

    def _prompt(self, **kwargs):
        args = {'count': 10, 'media_type': 'movie', 'mode': 'normal'}
        args.update(kwargs)
        return build_swipe_batch_prompt(**args)

    def test_novelty_levels(self):
        self.assertIn('favour well-known, popular', self._prompt(novelty='familiar'))
        self.assertIn('hidden gems', self._prompt(novelty='bold'))
        self.assertIn('mix a few well-known titles', self._prompt(novelty='balanced'))
        self.assertIn('mix a few well-known titles', self._prompt())

    def test_high_seen_ratio_warns_except_in_familiar_mode(self):
        self.assertIn('already seen 46% of the recent cards', self._prompt(seen_ratio=0.46))
        self.assertIn('already seen 46%', self._prompt(novelty='bold', seen_ratio=0.46))
        self.assertNotIn('of the recent cards', self._prompt(novelty='familiar', seen_ratio=0.46))
        self.assertNotIn('of the recent cards', self._prompt(seen_ratio=0.2))

    def test_novelty_does_not_apply_to_calibration(self):
        self.assertNotIn('NOVELTY', self._prompt(mode='calibration', novelty='bold'))

    def test_recalibration_targets_uncovered_ground(self):
        prompt = self._prompt(mode='calibration', recent_votes=VOTES)
        self.assertIn('recalibrates', prompt)
        self.assertIn('do NOT cover yet', prompt)
        self.assertNotIn('The user is new', prompt)

    def test_library_titles_are_listed_and_excluded(self):
        prompt = self._prompt(library_titles=[{'title': 'Inception', 'year': 2010, 'media_type': 'movie'}])
        self.assertIn('ALREADY IN THEIR LIBRARY', prompt)
        self.assertIn('- Inception (movie, 2010)', prompt)
        self.assertIn('already in their library', prompt)
