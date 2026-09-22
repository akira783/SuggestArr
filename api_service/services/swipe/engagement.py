"""Turn raw media-server engagement data into taste signals for Swipe prompts.

The media clients report facts (episodes watched, progress, play count, last played);
this module decides what they mean ("completed", "abandoned", ...) and which titles
say the most about the user's taste. It is pure so it can be tested without a server.
"""

from datetime import datetime, timezone

RECENT_DAYS = 30
ABANDONED_AFTER_DAYS = 60
COMPLETED_RATIO = 0.9
MOSTLY_WATCHED_RATIO = 0.6
ABANDONED_RATIO = 0.5
# A series is only "dropped" if little was invested: 35 episodes of a 100-episode show
# paused for months is a fan taking a break, not a rejection.
ABANDONED_MAX_EPISODES = 5
FINISHED_MOVIE_PCT = 90

# Engagement labels, from strongest positive signal to strongest negative one.
REWATCHED = 'rewatched'
COMPLETED = 'completed'
MOSTLY_WATCHED = 'mostly_watched'
WATCHED = 'watched'
IN_PROGRESS = 'in_progress'
PARTIALLY_WATCHED = 'partially_watched'
ABANDONED = 'abandoned'

POSITIVE_LABELS = {REWATCHED, COMPLETED, MOSTLY_WATCHED, WATCHED, IN_PROGRESS}
NEGATIVE_LABELS = {ABANDONED}


def _parse_date(value):
    """Parse a media-server ISO timestamp into an aware datetime, or None."""
    if not value:
        return None
    text = str(value).replace('Z', '+00:00')
    # Jellyfin/Emby send 7 fractional digits; datetime accepts at most 6.
    if '.' in text:
        head, _, tail = text.partition('.')
        digits = ''.join(ch for ch in tail if ch.isdigit())
        offset = tail[len(digits):]
        text = f"{head}.{digits[:6]}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_since(value, now):
    parsed = _parse_date(value)
    return None if parsed is None else (now - parsed).days


def classify_engagement(item, now=None):
    """Return the engagement label for one title reported by a media client.

    :param item: Dict as returned by ``JellyfinClient.get_engagement_items``.
    :param now: Reference time (aware datetime), defaults to the current UTC time.
    :return: One of the label constants of this module.
    """
    now = now or datetime.now(timezone.utc)
    days = _days_since(item.get('last_played'), now)
    recent = days is not None and days <= RECENT_DAYS
    stale = days is not None and days > ABANDONED_AFTER_DAYS

    if item.get('media_type') == 'tv':
        watched = item.get('episodes_watched') or 0
        total = item.get('episodes_total')
        ratio = watched / total if total else None
        if ratio is not None and ratio >= COMPLETED_RATIO:
            return COMPLETED
        # 4 of 5 episodes is nearly finished, not dropped, however long ago.
        if ratio is not None and ratio >= MOSTLY_WATCHED_RATIO:
            return MOSTLY_WATCHED
        if recent:
            return IN_PROGRESS
        few_watched = watched <= ABANDONED_MAX_EPISODES and (ratio is None or ratio < ABANDONED_RATIO)
        if stale and few_watched:
            return ABANDONED
        return PARTIALLY_WATCHED

    # PlayCount is not trusted as a re-watch signal: with remote/debrid playback every
    # restart increments it (9 "plays" for 0.9 h actually watched was observed), so a
    # "rewatched" label would mislead the LLM. REWATCHED is kept for sources that
    # report real completed views.
    if item.get('rewatched'):
        return REWATCHED
    if item.get('played') or (item.get('progress_pct') or 0) >= FINISHED_MOVIE_PCT:
        return WATCHED
    if recent:
        return IN_PROGRESS
    return ABANDONED if days is not None else PARTIALLY_WATCHED


def _weight(item, label, now):
    """Rank how much a title tells about taste: effort invested, then recency."""
    if item.get('media_type') == 'tv':
        effort = min(item.get('episodes_watched') or 0, 50)
    else:
        effort = 3 if item.get('rewatched') else (1 if item.get('played') else 0)
    if label in NEGATIVE_LABELS:
        # Dropped titles are informative too, but should not crowd out favourites.
        effort = max(effort, 1) * 0.5
    days = _days_since(item.get('last_played'), now)
    recency = 1.0 if days is None else max(0.2, 1 - days / 365)
    return effort * recency


def summarize_engagement(items, limit=40, now=None):
    """Label and rank titles by what they reveal about the user's taste.

    :param items: Raw engagement dicts from a media client.
    :param limit: Maximum number of titles returned.
    :param now: Reference time (aware datetime), defaults to the current UTC time.
    :return: List of dicts with ``title``, ``year``, ``media_type``, ``tmdb_id``,
        ``genres``, ``engagement`` (label) and ``detail`` (short human-readable
        evidence such as "35/100 episodes"), strongest signals first.
    """
    now = now or datetime.now(timezone.utc)
    ranked = []
    for item in items or []:
        if not item.get('title'):
            continue
        label = classify_engagement(item, now)
        ranked.append((_weight(item, label, now), item, label))
    ranked.sort(key=lambda entry: entry[0], reverse=True)

    summary = []
    for _, item, label in ranked[:limit]:
        summary.append({
            'title': item['title'],
            'year': item.get('year'),
            'media_type': item.get('media_type'),
            'tmdb_id': item.get('tmdb_id'),
            'genres': item.get('genres') or [],
            'engagement': label,
            'detail': _detail(item),
        })
    return summary


def _detail(item):
    if item.get('media_type') == 'tv':
        watched = item.get('episodes_watched') or 0
        total = item.get('episodes_total')
        return f"{watched}/{total} episodes" if total else f"{watched} episodes"
    if item.get('rewatched'):
        return 'watched several times'
    if item.get('progress_pct') and not item.get('played'):
        return f"stopped at {item['progress_pct']}%"
    return 'watched'
