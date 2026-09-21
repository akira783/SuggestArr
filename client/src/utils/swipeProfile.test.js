import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isProfileDirty, percent, profileOrigin, statsRows } from './swipeProfile.js';

test('percent', () => {
  assert.equal(percent(0.6), '60%');
  assert.equal(percent(0.125), '13%');
  assert.equal(percent(0), '0%');
  assert.equal(percent(undefined), '—');
});

test('no rows before the first vote', () => {
  assert.deepEqual(statsRows(null), []);
  assert.deepEqual(statsRows({ total: 0 }), []);
});

test('rows include per pick type like rates when available', () => {
  const rows = statsRows({
    total: 5, likes: 3, dislikes: 2, requested: 1, like_rate: 0.6, request_rate: 0.5,
    by_pick_type: { safe: { total: 1, likes: 1 }, explore: { total: 2, likes: 1 },
      calibration: { total: 2, likes: 1 } },
  });
  assert.deepEqual(rows, [
    { label: 'Cards answered', value: '5' },
    { label: 'Liked', value: '3 (60%)' },
    { label: 'Likes requested', value: '1 (50%)' },
    { label: 'Liked — safe picks', value: '100%' },
    { label: 'Liked — wildcards', value: '50%' },
  ]);
});

test('rows skip pick types never shown', () => {
  const rows = statsRows({ total: 2, likes: 1, requested: 0, like_rate: 0.5, request_rate: 0,
    by_pick_type: { calibration: { total: 2, likes: 1 } } });
  assert.equal(rows.length, 3);
});

test('dirty only when the trimmed text changed and is not empty', () => {
  assert.equal(isProfileDirty('Loves sci-fi.', 'Loves sci-fi.'), false);
  assert.equal(isProfileDirty('  Loves sci-fi.  ', 'Loves sci-fi.'), false);
  assert.equal(isProfileDirty('Loves sci-fi. No superheroes.', 'Loves sci-fi.'), true);
  assert.equal(isProfileDirty('   ', 'Loves sci-fi.'), false);
  assert.equal(isProfileDirty('First text', null), true);
});

test('origin label', () => {
  assert.equal(profileOrigin(null), null);
  assert.match(profileOrigin({ user_edited: true }), /Edited by you/);
  assert.match(profileOrigin({ user_edited: false }), /Written by the AI/);
});
