// Pure helpers for the Swipe card deck, kept out of the component so they can be tested.

export const SWIPE_THRESHOLD = 0.3;
export const PREFETCH_WHEN_LEFT = 3;

export function cardKey(card) {
  return `${card.media_type}-${card.id}`;
}

/** Decide what a released drag means: 'like', 'dislike' or null (snap back). */
export function swipeDecision(dx, width, threshold = SWIPE_THRESHOLD) {
  if (!width || !Number.isFinite(dx)) return null;
  if (dx > width * threshold) return 'like';
  if (dx < -width * threshold) return 'dislike';
  return null;
}

/** Rotation (degrees) of a card dragged by dx pixels, capped for readability. */
export function dragRotation(dx, width) {
  if (!width) return 0;
  return Math.max(-15, Math.min(15, (dx / width) * 20));
}

const KEY_ACTIONS = { ArrowRight: 'like', ArrowLeft: 'dislike', ArrowDown: 'seen' };

export function keyToAction(key) {
  return KEY_ACTIONS[key] ?? null;
}

/** Append incoming cards, skipping any already in the deck or already answered. */
export function mergeCards(deck, incoming, answeredKeys = new Set()) {
  const known = new Set([...deck.map(cardKey), ...answeredKeys]);
  const added = [];
  for (const card of incoming || []) {
    const key = cardKey(card);
    if (known.has(key)) continue;
    known.add(key);
    added.push(card);
  }
  return deck.concat(added);
}

export function needsMore(deckLength, position, threshold = PREFETCH_WHEN_LEFT) {
  return deckLength - position <= threshold;
}

export function cardTitle(card) {
  return card.title || card.name || '';
}

export function cardYear(card) {
  if (card.year) return card.year;
  const date = card.release_date || card.first_air_date || '';
  return /^\d{4}/.test(date) ? Number(date.slice(0, 4)) : null;
}

export function pickLabel(pickType) {
  if (pickType === 'explore') return 'Wildcard';
  if (pickType === 'calibration') return 'Calibration';
  return null;
}

/** "On Netflix", "On Netflix +2", or null when the title is not streaming. */
export function streamingLabel(streaming) {
  const providers = streaming?.providers || [];
  if (!providers.length) return null;
  const extra = providers.length > 1 ? ` +${providers.length - 1}` : '';
  return `On ${providers[0].name}${extra}`;
}

export function mediaNoun(card) {
  return card.media_type === 'tv' ? 'series' : 'movie';
}

/** Toast text for the request_status returned by /api/swipe/request. */
export function requestMessage(status, title) {
  if (status === 'awaiting_approval') return `${title} is waiting for approval in Requests.`;
  if (status === 'queued') return `${title} has been requested.`;
  if (status === 'already_requested') return `${title} was already requested.`;
  return `${title}: request sent.`;
}
