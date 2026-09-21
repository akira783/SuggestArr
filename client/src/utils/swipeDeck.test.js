import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  cardKey, cardYear, dragRotation, keyToAction, mergeCards, needsMore, pickLabel, sleep,
  requestMessage, streamingLabel, swipeDecision,
} from './swipeDeck.js';

const dune = { id: 438631, media_type: 'movie', title: 'Dune', release_date: '2021-09-15' };
const dark = { id: 70523, media_type: 'tv', name: 'Dark', first_air_date: '2017-12-01' };

test('a drag past 30% of the width decides, a shorter one snaps back', () => {
  assert.equal(swipeDecision(130, 400), 'like');
  assert.equal(swipeDecision(-130, 400), 'dislike');
  assert.equal(swipeDecision(100, 400), null);
  assert.equal(swipeDecision(-100, 400), null);
  assert.equal(swipeDecision(500, 0), null);
  assert.equal(swipeDecision(Number.NaN, 400), null);
});

test('rotation follows the drag and is capped', () => {
  assert.equal(dragRotation(0, 400), 0);
  assert.equal(dragRotation(100, 400), 5);
  assert.equal(dragRotation(4000, 400), 15);
  assert.equal(dragRotation(-4000, 400), -15);
  assert.equal(dragRotation(50, 0), 0);
});

test('keyboard shortcuts', () => {
  assert.equal(keyToAction('ArrowRight'), 'like');
  assert.equal(keyToAction('ArrowLeft'), 'dislike');
  assert.equal(keyToAction('ArrowDown'), 'seen');
  assert.equal(keyToAction('Enter'), null);
});

test('merging skips duplicates and answered cards; movie and TV ids do not collide', () => {
  const sameIdTv = { id: 438631, media_type: 'tv', name: 'Other' };
  const merged = mergeCards([dune], [dune, dark, sameIdTv, dark], new Set(['tv-438631']));
  assert.deepEqual(merged.map(cardKey), ['movie-438631', 'tv-70523']);
  assert.deepEqual(mergeCards([], undefined), []);
});

test('more cards are fetched when three or fewer are left', () => {
  assert.equal(needsMore(10, 6), false);
  assert.equal(needsMore(10, 7), true);
  assert.equal(needsMore(0, 0), true);
});

test('card details', () => {
  assert.equal(cardYear(dune), 2021);
  assert.equal(cardYear(dark), 2017);
  assert.equal(cardYear({ year: 1999 }), 1999);
  assert.equal(cardYear({ release_date: '' }), null);
  assert.equal(pickLabel('explore'), 'Wildcard');
  assert.equal(pickLabel('calibration'), 'Calibration');
  assert.equal(pickLabel('safe'), null);
});

test('streaming badge text', () => {
  assert.equal(streamingLabel(null), null);
  assert.equal(streamingLabel({ providers: [] }), null);
  assert.equal(streamingLabel({ providers: [{ name: 'Netflix' }] }), 'On Netflix');
  assert.equal(streamingLabel({ providers: [{ name: 'Netflix' }, { name: 'Molotov TV' }] }),
    'On Netflix +1');
});

test('request feedback follows the request status', () => {
  assert.equal(requestMessage('awaiting_approval', 'Dune'), 'Dune is waiting for approval in Requests.');
  assert.equal(requestMessage('queued', 'Dune'), 'Dune has been requested.');
  assert.equal(requestMessage('already_requested', 'Dune'), 'Dune was already requested.');
});

test('sleep resolves after the delay', async () => {
  const start = Date.now();
  await sleep(20);
  assert.ok(Date.now() - start >= 15);
});
