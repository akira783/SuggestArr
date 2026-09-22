// Pure helpers for the Swipe card deck, kept out of the component so they can be tested.

export const SWIPE_THRESHOLD = 0.3;
export const PREFETCH_WHEN_LEFT = 3;
// Batches and profile rewrites are generated in the background; the page polls.
export const POLL_INTERVAL_MS = 1500;
export const MAX_POLLS = 60;

export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

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

// Answers: like / dislike, or "already seen" liked / disliked in one step.
const KEY_ACTIONS = {
  ArrowRight: 'like', ArrowLeft: 'dislike', ArrowUp: 'seen_liked', ArrowDown: 'seen_disliked',
};

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

export const NOVELTY_OPTIONS = [
  { value: 'familiar', label: 'Familiar', icon: 'fas fa-couch', hint: 'Well-known hits in your taste' },
  { value: 'balanced', label: 'Balanced', icon: 'fas fa-balance-scale', hint: 'A mix of known and less obvious titles' },
  { value: 'bold', label: 'Surprise me', icon: 'fas fa-dice', hint: 'Hidden gems and titles you have probably not seen' },
];

export function isNovelty(value) {
  return NOVELTY_OPTIONS.some(option => option.value === value);
}

/** Privacy-friendly YouTube embed for a trailer returned by the API, or null. */
export function trailerEmbedUrl(trailer) {
  if (!trailer || !/^[\w-]{6,20}$/.test(trailer.key || '')) return null;
  return `https://www.youtube-nocookie.com/embed/${trailer.key}?autoplay=1&rel=0`;
}

/** Where a vote sends the card when it leaves the screen (for the exit animation). */
export function exitDirection(vote) {
  if (vote === 'like') return { x: 1, y: 0 };
  if (vote === 'dislike') return { x: -1, y: 0 };
  if (vote === 'seen_liked') return { x: 0, y: -1 };
  return { x: 0, y: 1 };
}
