"""Optional signal sources for Swipe, built on the connectors SuggestArr already has.

Every source is used only when it is configured, and a missing or failing source never
blocks a batch of cards: it is logged as a warning and simply left out.

- Media server engagement (Jellyfin / Emby): episodes watched, progress, re-watches.
- TMDb watch providers: where a card is already streaming, for the card badge.
- OMDb: IMDb / Rotten Tomatoes / Metacritic ratings on the card.
"""

import asyncio
import json
import threading
import time

from api_service.config.logger_manager import LoggerManager
from api_service.services.swipe.engagement import summarize_engagement

logger = LoggerManager.get_logger("SwipeSignals")

ENRICH_CONCURRENCY = 5
LIBRARY_CACHE_SECONDS = 600

# Library contents change rarely and listing them costs a full scan: cache per process.
_library_cache = {'key': None, 'items': (), 'at': 0.0}
_library_lock = threading.Lock()


class SwipeSignals:
    """Collects Swipe signals from whichever connectors are configured."""

    def __init__(self, config):
        """
        :param config: Runtime configuration dict (``ConfigService.get_runtime_config()``).
        """
        self.config = config or {}

    @property
    def streaming_region(self):
        """ISO country code used for streaming badges, or None when not configured."""
        region = self.config.get('FILTER_REGION_PROVIDER')
        return str(region).upper() if region else None

    @property
    def user_service_ids(self):
        """Provider ids of the services the user declared (FILTER_STREAMING_SERVICES)."""
        services = self.config.get('FILTER_STREAMING_SERVICES') or []
        return {str(service.get('provider_id')) for service in services
                if isinstance(service, dict) and service.get('provider_id') is not None}

    async def engagement(self, media_users, limit=40):
        """Summarise how the given media-server users engaged with their library.

        :param media_users: List of media-server users (dicts with 'id' and 'name', or
            plain ids). Empty means "no linked profile": nothing is fetched, so one
            account never sees another account's history.
        :param limit: Maximum number of titles in the summary.
        :return: List from ``summarize_engagement``; empty when the media server is
            not Jellyfin/Emby, not configured, or unreachable.
        """
        service = str(self.config.get('SELECTED_SERVICE') or '').lower()
        if service not in ('jellyfin', 'emby') or not media_users:
            return []
        api_url = self.config.get('JELLYFIN_API_URL')
        token = self.config.get('JELLYFIN_TOKEN')
        if not api_url or not token:
            return []

        from api_service.services.jellyfin.jellyfin_client import JellyfinClient

        libraries = self.config.get('JELLYFIN_LIBRARIES')
        client = JellyfinClient(
            api_url=api_url,
            token=token,
            library_ids=libraries if isinstance(libraries, list) and libraries else None,
        )
        items = []
        try:
            async with client:
                for user in media_users:
                    user = user if isinstance(user, dict) else {'id': user, 'name': str(user)}
                    items.extend(await client.get_engagement_items(user))
        except Exception as exc:
            logger.warning("Engagement signal unavailable: %s", exc)
            return []
        return summarize_engagement(items, limit=limit)

    async def library_items(self):
        """Titles already in the media library, as dicts with ``tmdb_id``, ``media_type``,
        ``title`` and ``year``.

        Used to keep cards to titles the user does not have yet. Cached for
        ``LIBRARY_CACHE_SECONDS``; empty when the media server is not Jellyfin/Emby,
        not configured, or unreachable.
        """
        service = str(self.config.get('SELECTED_SERVICE') or '').lower()
        api_url, token = self.config.get('JELLYFIN_API_URL'), self.config.get('JELLYFIN_TOKEN')
        if service not in ('jellyfin', 'emby') or not api_url or not token:
            return []
        libraries = self.config.get('JELLYFIN_LIBRARIES')
        libraries = libraries if isinstance(libraries, list) and libraries else None
        cache_key = (api_url, json.dumps(libraries, sort_keys=True, default=str))
        with _library_lock:
            if (_library_cache['key'] == cache_key
                    and time.monotonic() - _library_cache['at'] < LIBRARY_CACHE_SECONDS):
                return list(_library_cache['items'])

        from api_service.services.jellyfin.jellyfin_client import JellyfinClient
        client = JellyfinClient(api_url=api_url, token=token, library_ids=libraries)
        try:
            async with client:
                found = await client.get_all_library_items() or {}
        except Exception as exc:
            logger.warning("Library contents unavailable: %s", exc)
            return []
        items = tuple(
            {'tmdb_id': str(item['tmdb_id']), 'media_type': media_type,
             'title': item.get('Name'), 'year': item.get('ProductionYear')}
            for media_type in ('movie', 'tv')
            for item in found.get(media_type, [])
            if item.get('tmdb_id')
        )
        with _library_lock:
            _library_cache.update(key=cache_key, items=items, at=time.monotonic())
        return list(items)

    async def library_keys(self):
        """``(tmdb_id, media_type)`` pairs already in the media library."""
        return {(item['tmdb_id'], item['media_type']) for item in await self.library_items()}

    async def enrich_cards(self, cards, language='en'):
        """Add streaming availability, external ratings and a trailer to cards, in place.

        Each card gains ``streaming`` (dict from ``TMDbClient.get_streaming_availability``
        plus ``on_user_services``, or None), ``ratings`` (dict from
        ``OmdbClient.get_ratings``, or None) and ``trailer`` (dict from
        ``TMDbClient.get_trailer``, or None).

        :param cards: List of card dicts with at least 'id' (TMDb id) and 'media_type'.
        :param language: Reader's TMDb language, to prefer a trailer they understand.
        :return: The same list, for chaining.
        """
        region = self.streaming_region
        omdb_key = self.config.get('OMDB_API_KEY')
        tmdb_key = self.config.get('TMDB_API_KEY')
        for card in cards:
            card.setdefault('streaming', None)
            card.setdefault('ratings', None)
            card.setdefault('trailer', None)
        if not cards or not tmdb_key:
            return cards

        from api_service.services.tmdb.tmdb_client import TMDbClient
        tmdb = TMDbClient(
            api_key=tmdb_key, search_size=20, tmdb_threshold=0, tmdb_min_votes=0,
            include_no_ratings=True, filter_release_year=0, filter_language=[],
            filter_genre=[], filter_region_provider=None, filter_streaming_services=None,
        )
        omdb = None
        if omdb_key:
            from api_service.services.omdb.omdb_client import OmdbClient
            omdb = OmdbClient(omdb_key)

        user_services = self.user_service_ids
        semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY)

        async def enrich(card):
            async with semaphore:
                content_id, media_type = card.get('id'), card.get('media_type')
                if not content_id or media_type not in ('movie', 'tv'):
                    return
                if region:
                    try:
                        streaming = await tmdb.get_streaming_availability(content_id, media_type, region)
                    except Exception as exc:
                        logger.warning("Streaming badge unavailable for %s: %s", content_id, exc)
                        streaming = None
                    if streaming:
                        streaming['on_user_services'] = any(
                            str(provider['id']) in user_services for provider in streaming['providers']
                        )
                    card['streaming'] = streaming
                try:
                    card['trailer'] = await tmdb.get_trailer(content_id, media_type, language)
                except Exception as exc:
                    logger.warning("Trailer unavailable for %s: %s", content_id, exc)
                if omdb:
                    try:
                        imdb_id = await tmdb.get_imdb_id(content_id, media_type)
                        card['ratings'] = await omdb.get_ratings(imdb_id) if imdb_id else None
                    except Exception as exc:
                        logger.warning("Ratings unavailable for %s: %s", content_id, exc)

        try:
            await asyncio.gather(*(enrich(card) for card in cards))
        finally:
            await tmdb.close()
            if omdb:
                await omdb.close()
        return cards
