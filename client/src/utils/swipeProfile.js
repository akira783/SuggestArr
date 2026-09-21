// Pure helpers for the Swipe "My taste" panel.

export const PROFILE_MAX_LENGTH = 4000;

export function percent(rate) {
  if (!Number.isFinite(rate)) return '—';
  return `${Math.round(rate * 100)}%`;
}

function pickRate(stats, pickType) {
  const bucket = stats?.by_pick_type?.[pickType];
  return bucket && bucket.total ? bucket.likes / bucket.total : null;
}

/** Rows shown in the panel, from GET /api/swipe/stats. Empty until the first vote. */
export function statsRows(stats) {
  if (!stats || !stats.total) return [];
  const rows = [
    { label: 'Cards answered', value: String(stats.total) },
    { label: 'Liked', value: `${stats.likes} (${percent(stats.like_rate)})` },
    { label: 'Likes requested', value: `${stats.requested} (${percent(stats.request_rate)})` },
  ];
  const safe = pickRate(stats, 'safe');
  const wildcard = pickRate(stats, 'explore');
  if (safe !== null) rows.push({ label: 'Liked — safe picks', value: percent(safe) });
  if (wildcard !== null) rows.push({ label: 'Liked — wildcards', value: percent(wildcard) });
  return rows;
}

/** Whether the edited text differs from the saved profile in a way worth saving. */
export function isProfileDirty(draft, saved) {
  const text = (draft || '').trim();
  return text.length > 0 && text !== (saved || '').trim();
}

export function profileOrigin(profile) {
  if (!profile) return null;
  return profile.user_edited ? 'Edited by you — the AI keeps your corrections' : 'Written by the AI from your votes';
}
