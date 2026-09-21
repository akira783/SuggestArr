"""
This module provides the JellyfinClient class for interacting with the Jellyfin API.
The client can retrieve users, recent items, and provider IDs for media content.

Classes:
    - JellyfinClient: A class that handles communication with the Jellyfin API.
"""
import aiohttp
import asyncio
from api_service.services.http.base_client import BaseHTTPClient
from api_service.config.logger_manager import LoggerManager

# Constants
LIBRARY_FETCH_TIMEOUT = 120  # Timeout in seconds for full-library bulk fetches


class JellyfinClient(BaseHTTPClient):
    """
    A client to interact with the Jellyfin API, allowing the retrieval of users, recent items,
    and media provider IDs.
    """

    def __init__(self, api_url, token, max_content=10, library_ids=None):
        """
        Initializes the JellyfinClient with the provided API URL and token.
        :param api_url: The base URL for the Jellyfin API.
        :param token: The authentication token for Jellyfin.
        """
        super().__init__()
        self.max_content_fetch = int(max_content)
        # Strip trailing slash and whitespace so URL joins never produce double-slash paths.
        self.api_url = api_url.rstrip('/').strip() if api_url else api_url
        self.libraries = library_ids
        # Strip whitespace from token to avoid 401s from copy-paste artefacts.
        self.api_token = token.strip() if token else token
        self.headers = {
            "X-Emby-Token": self.api_token,
            "Authorization": f'MediaBrowser Token="{self.api_token}"'
        }
        self.existing_content = {}
        self._series_provider_ids_cache = {}

    async def init_existing_content(self):
        self.logger.info('Initializing existing content.')
        self.existing_content = await self.get_all_library_items()
        self.logger.debug(f'Existing content initialized: {self.existing_content}')

    async def get_all_users(self):
        """
        Retrieves a list of all users from the Jellyfin server asynchronously.
        :return: A list of users in JSON format if successful, otherwise an empty list.
        """
        url = f"{self.api_url}/Users"
        self.logger.debug(f'Requesting all users from {url}')
        try:
            session = await self._get_session()
            async with session.get(url, headers=self.headers, timeout=self.REQUEST_TIMEOUT) as response:
                if response.status == 200:
                    data = await response.json()
                    self.logger.debug(f'Users retrieved: {data}')
                    return [{"id": user["Id"], "name": user["Name"], "policy": user["Policy"]} for user in data]
                self.logger.error("Failed to retrieve users: %d", response.status)
        except aiohttp.ClientError as e:
            self.logger.error("An error occurred while retrieving users: %s", str(e))

        return []

    async def get_all_library_items(self):
        """
        Retrieves all Movie and Series items from the specified libraries.
        Returns a dict with keys: 'movie' and 'tv'.
        """
        results_by_library = {"movie": [], "tv": []}

        if not self.libraries:
            raw = await self.get_libraries() or []
            # get_libraries() returns raw Jellyfin VirtualFolders format (ItemId/Name).
            # Normalize to the internal {id, name} shape expected by all callers so
            # that get_recent_items() works correctly when libraries weren't pre-configured.
            self.libraries = [
                {"id": lib["ItemId"], "name": lib["Name"]}
                for lib in raw
                if lib.get("ItemId") and lib.get("Name")
            ]
        libraries = self.libraries
        self.logger.debug(f"Libraries type: {type(libraries)}, content: {libraries}")

        if not libraries:
            self.logger.error("No libraries found.")
            return None

        session = await self._get_session()
        tasks = [
            self._fetch_library_items(session, library, results_by_library)
            for library in libraries
        ]
        await asyncio.gather(*tasks)

        return results_by_library

    async def _fetch_library_items(self, session, library, results_by_library):
        """
        Fetch items for a single library and update results_by_library.
        """
        library_id = library.get('id') if isinstance(library, dict) else None
        library_name = library.get('name') if isinstance(library, dict) else None

        if not library_id:
            self.logger.error(f"Library item is missing 'id': {library}")
            return

        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Series",
            "Fields": "ProviderIds",
            "ParentID": library_id
        }

        self.logger.debug(f"Requesting items for library {library_name} with params: {params}")

        try:
            async with session.get(
                f"{self.api_url}/Items",
                headers=self.headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=LIBRARY_FETCH_TIMEOUT)
            ) as response:
                if response.status != 200:
                    self.logger.error("Failed to get items for library %s: %d", library_name, response.status)
                    return

                data = await response.json()
                items = data.get("Items", [])

                added = 0
                for item in items:
                    item_type = item.get("Type")
                    if item_type not in ("Movie", "Series"):
                        continue

                    tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
                    if not tmdb_id:
                        continue

                    item["tmdb_id"] = tmdb_id
                    bucket = "tv" if item_type == "Series" else "movie"
                    results_by_library[bucket].append(item)
                    added += 1

                self.logger.info("Retrieved %d valid items in %s", added, library_name)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.logger.error("Error retrieving items for library %s: %s", library_name, str(e))

        return results_by_library

    async def get_recent_items(self, user):
        """
        Retrieves a list of recently played items for a given user from specific libraries asynchronously.
        Uses the user-scoped endpoint /Users/{userId}/Items to respect per-user library visibility.
        Falls back to a simpler query if the primary request returns 404 (e.g. Collections library).
        :param user: Dict with 'id' and 'name' keys for the target user.
        :return: A combined dict of recent items keyed by library name, or None if nothing retrieved.
        """
        results_by_library = {}
        seen_series = set()  # Track seen series to avoid duplicates

        user_id = user['id']
        user_name = user.get('name', user_id)

        self.logger.debug(f'Fetching recent items for user {user_name} (id={user_id})')

        for library in self.libraries:
            library_id = library.get('id')
            library_name = library.get('name', library_id)

            # Use the proper user-scoped endpoint so Jellyfin enforces per-user library
            # visibility correctly. Using /Items?userId=... on the admin endpoint caused
            # 404 for users whose library visibility excludes certain virtual folders
            # (e.g. Collections) in Jellyfin 10.9+.
            url = f"{self.api_url}/Users/{user_id}/Items"
            # Over-fetch from the API by a factor of 10 so that the client-side
            # per-series deduplication below can still yield max_content_fetch
            # *unique* series even when the user recently binge-watched a few shows
            # (each episode would otherwise consume one Limit slot, leaving very
            # few unique series after dedup if the Limit equals max_content_fetch).
            api_fetch_limit = max(self.max_content_fetch * 5, 100)
            params = {
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
                "IsPlayed": "true",
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Limit": api_fetch_limit,
                "ParentId": library_id,
                "Fields": "ProviderIds,SeriesProviderIds",
                "EnableUserData": "true",
            }

            self.logger.debug(
                "[library=%s] [userId=%s] GET %s params=%s",
                library_name, user_id, url, params
            )

            try:
                session = await self._get_session()
                async with session.get(url, headers=self.headers, params=params, timeout=self.REQUEST_TIMEOUT) as response:
                    if response.status == 200:
                        library_items = await response.json()
                        items = library_items.get('Items', [])
                        self.logger.debug(
                            "[library=%s] [user=%s] Raw item count: %d",
                            library_name, user_name, len(items)
                        )

                        filtered_items = []
                        for item in items:
                            if len(filtered_items) >= int(self.max_content_fetch):
                                break
                            if item['Type'] == 'Episode':
                                series_title = item['SeriesName']
                                if series_title not in seen_series:
                                    seen_series.add(series_title)
                                    filtered_items.append(item)
                            else:
                                filtered_items.append(item)

                        results_by_library[library_name] = filtered_items
                        self.logger.info(
                            "Retrieved %d watched items in %s for user %s",
                            len(filtered_items), library_name, user_name
                        )

                    elif response.status == 404:
                        raw_body = await response.text()
                        self.logger.warning(
                            "[library=%s] [user=%s] Recent items returned 404 — library may not be "
                            "visible to this user or does not support DatePlayed sorting. "
                            "URL=%s body=%.300s. Trying fallback query.",
                            library_name, user_name, url, raw_body
                        )
                        fallback_items = await self._fallback_recent_items(
                            user_id, user_name, library_id, library_name
                        )
                        if fallback_items is not None:
                            results_by_library[library_name] = fallback_items

                    else:
                        self.logger.error(
                            "Failed to get recent items for library %s (user %s): %d",
                            library_name, user_name, response.status
                        )

            except aiohttp.ClientError as e:
                self.logger.error(
                    "An error occurred while retrieving recent items for library %s (user %s): %s",
                    library_name, user_name, str(e)
                )
            except Exception as e:
                self.logger.error(
                    "Unexpected error retrieving recent items for library %s (user %s): %s",
                    library_name, user_name, str(e)
                )

        return results_by_library if results_by_library else None

    async def _fallback_recent_items(self, user_id, user_name, library_id, library_name):
        """
        Fallback for when the primary recent-items query returns 404.
        Omits SortBy=DatePlayed, which some virtual libraries (e.g. Collections) do not support.
        Uses /Users/{userId}/Items?ParentId=...&IsPlayed=true without date sorting.
        :return: List of items, or None if the fallback also fails.
        """
        url = f"{self.api_url}/Users/{user_id}/Items"
        api_fetch_limit = max(self.max_content_fetch * 5, 100)
        params = {
            "IsPlayed": "true",
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Limit": api_fetch_limit,
            "ParentId": library_id,
            "Fields": "ProviderIds,SeriesProviderIds",
            "EnableUserData": "true",
        }

        self.logger.debug(
            "[library=%s] [userId=%s] Fallback GET %s params=%s",
            library_name, user_id, url, params
        )

        try:
            session = await self._get_session()
            async with session.get(url, headers=self.headers, params=params, timeout=self.REQUEST_TIMEOUT) as response:
                if response.status == 200:
                    data = await response.json()
                    items = data.get('Items', [])
                    self.logger.info(
                        "[library=%s] [user=%s] Fallback retrieved %d items",
                        library_name, user_name, len(items)
                    )
                    return items
                else:
                    raw_body = await response.text()
                    self.logger.warning(
                        "[library=%s] [user=%s] Fallback also failed: %d body=%.300s — skipping library.",
                        library_name, user_name, response.status, raw_body
                    )
                    return None
        except Exception as e:
            self.logger.error(
                "[library=%s] [user=%s] Fallback error: %s",
                library_name, user_name, str(e)
            )
            return None

    async def get_engagement_items(self, user):
        """
        Retrieves how much a user engaged with each movie and series, not only what is
        fully played.

        ``get_recent_items`` only sees items flagged as played, which misses series the
        user started and dropped, films stopped halfway, and re-watches. This method
        combines three user-scoped queries per library:

        - movies and series with their user data (play count, progress, unplayed
          episode count),
        - played episodes, aggregated per series,
        - the resume list (items currently in progress).

        :param user: Dict with 'id' and 'name' keys for the target user.
        :return: List of dicts with ``title``, ``year``, ``media_type`` ('movie'|'tv'),
            ``tmdb_id``, ``genres``, ``play_count``, ``played``, ``progress_pct``,
            ``last_played`` (ISO string or None), ``episodes_watched``,
            ``episodes_total`` and ``in_progress``. Items the user never touched are
            left out. Returns an empty list on errors.
        """
        user_id = user['id']
        user_name = user.get('name', user_id)
        libraries = self.libraries or [{'id': None, 'name': 'all'}]
        items_url = f"{self.api_url}/Users/{user_id}/Items"

        titles = {}
        episodes = {}
        for library in libraries:
            library_params = {'ParentId': library['id']} if library.get('id') else {}
            base_params = {
                'Recursive': 'true',
                'EnableUserData': 'true',
                'Limit': 5000,
                **library_params,
            }
            for item in await self._get_items(items_url, {
                **base_params,
                'IncludeItemTypes': 'Movie,Series',
                'Fields': 'ProviderIds,Genres,ProductionYear',
            }, user_name):
                titles[item.get('Id')] = item
            for episode in await self._get_items(items_url, {
                **base_params,
                'IncludeItemTypes': 'Episode',
                'IsPlayed': 'true',
            }, user_name):
                series_id = episode.get('SeriesId')
                if not series_id:
                    continue
                stats = episodes.setdefault(series_id, {'count': 0, 'last_played': None})
                stats['count'] += 1
                stats['last_played'] = self._latest_date(
                    stats['last_played'], (episode.get('UserData') or {}).get('LastPlayedDate')
                )

        resume = {}
        for item in await self._get_items(
            f"{items_url}/Resume", {'Limit': 200, 'EnableUserData': 'true'}, user_name
        ):
            key = item.get('SeriesId') if item.get('Type') == 'Episode' else item.get('Id')
            user_data = item.get('UserData') or {}
            resume[key] = {
                'progress_pct': user_data.get('PlayedPercentage'),
                'last_played': user_data.get('LastPlayedDate'),
            }

        results = []
        for item_id, item in titles.items():
            user_data = item.get('UserData') or {}
            is_series = item.get('Type') == 'Series'
            watched = episodes.get(item_id, {}).get('count', 0) if is_series else None
            resumed = resume.get(item_id)
            play_count = int(user_data.get('PlayCount') or 0)
            played = bool(user_data.get('Played'))
            progress = (resumed or {}).get('progress_pct')
            if progress is None and not is_series:
                progress = user_data.get('PlayedPercentage')
            if not (watched or resumed or play_count or played or progress):
                continue
            last_played = self._latest_date(
                user_data.get('LastPlayedDate'),
                episodes.get(item_id, {}).get('last_played'),
                (resumed or {}).get('last_played'),
            )
            episodes_total = None
            if is_series and user_data.get('UnplayedItemCount') is not None:
                episodes_total = watched + int(user_data['UnplayedItemCount'])
            results.append({
                'title': item.get('Name'),
                'year': item.get('ProductionYear'),
                'media_type': 'tv' if is_series else 'movie',
                'tmdb_id': (item.get('ProviderIds') or {}).get('Tmdb'),
                'genres': item.get('Genres') or [],
                'play_count': play_count,
                'played': played,
                'progress_pct': round(progress) if progress is not None else None,
                'last_played': last_played,
                'episodes_watched': watched,
                'episodes_total': episodes_total,
                'in_progress': resumed is not None,
            })

        self.logger.info(
            "Retrieved engagement for %d titles for user %s", len(results), user_name
        )
        return results

    async def _get_items(self, url, params, user_name):
        """GET a Jellyfin item list and return its 'Items', or [] on any failure."""
        try:
            session = await self._get_session()
            async with session.get(url, headers=self.headers, params=params,
                                   timeout=self.REQUEST_TIMEOUT) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('Items', []) if isinstance(data, dict) else []
                self.logger.warning(
                    "Engagement query failed for user %s: HTTP %d (%s)",
                    user_name, response.status, url,
                )
        except aiohttp.ClientError as e:
            self.logger.warning("Engagement query error for user %s: %s", user_name, str(e))
        return []

    @staticmethod
    def _latest_date(*values):
        """Return the most recent of several ISO-8601 date strings, ignoring None."""
        dates = [value for value in values if value]
        # Jellyfin returns fixed-width UTC timestamps, so string order is date order.
        return max(dates) if dates else None

    async def get_series_provider_ids(self, series_id):
        """
        Retrieves provider IDs from a parent series item.
        Jellyfin episode items do not always include the series TMDb ID.
        """
        if not series_id:
            return {}

        if series_id in self._series_provider_ids_cache:
            return self._series_provider_ids_cache[series_id]

        url = f"{self.api_url}/Items"
        params = {"Fields": "ProviderIds", "Ids": series_id}
        self.logger.debug("Requesting series provider IDs for %s", series_id)

        try:
            session = await self._get_session()
            async with session.get(
                url,
                headers=self.headers,
                params=params,
                timeout=self.REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    self.logger.warning(
                        "Failed to get series provider IDs for %s: %d",
                        series_id,
                        response.status
                    )
                    return {}

                data = await response.json()
                items = data.get("Items") or []
                provider_ids = data.get("ProviderIds") or (
                    (items[0].get("ProviderIds") or {}) if items else {}
                )
                self._series_provider_ids_cache[series_id] = provider_ids
                return provider_ids
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.logger.error(
                "Error retrieving series provider IDs for %s: %s",
                series_id,
                str(e)
            )
            return {}

    async def get_libraries(self):
        """
        Retrieves list of libraries asynchronously.

        Tries /Library/VirtualFolders first (works with user session tokens on all
        Jellyfin versions). If that returns an empty list, falls back to
        /Library/MediaFolders which works with system API keys on Jellyfin 10.10+.
        The MediaFolders response is normalized to the VirtualFolders format
        (list of dicts with ItemId/Name/CollectionType keys).
        """
        self.logger.info("Searching Jellyfin libraries.")

        auth_attempts = [
            {
                "name": "X-Emby-Token header",
                "headers": {
                    "X-Emby-Token": self.api_token,
                    "Authorization": f'MediaBrowser Token="{self.api_token}"'
                },
                "params": None,
            },
            {
                "name": "MediaBrowser Authorization header",
                "headers": {"Authorization": f'MediaBrowser Token="{self.api_token}"'},
                "params": None,
            },
            {
                "name": "api_key query parameter",
                "headers": None,
                "params": {"api_key": self.api_token},
            },
        ]

        def _sanitize_headers(headers):
            if not headers:
                return {}
            sanitized = {}
            for key, value in headers.items():
                key_lower = key.lower()
                if key_lower.endswith("token") or key_lower == "authorization":
                    sanitized[key] = "***"
                else:
                    sanitized[key] = value
            return sanitized

        # Try /Library/VirtualFolders first
        libraries = await self._fetch_libraries_from_endpoint(
            "/Library/VirtualFolders", auth_attempts, _sanitize_headers
        )

        if libraries:
            return libraries

        # VirtualFolders returned empty or failed — fall back to /Library/MediaFolders
        # which works with system API keys on Jellyfin 10.10+.
        self.logger.info(
            "VirtualFolders returned no libraries, trying /Library/MediaFolders fallback."
        )
        libraries = await self._fetch_libraries_from_endpoint(
            "/Library/MediaFolders", auth_attempts, _sanitize_headers
        )

        if libraries is None:
            return None

        # Normalize MediaFolders response format.
        # MediaFolders returns {"Items": [...]} with "Id" keys,
        # while VirtualFolders returns a flat list with "ItemId" keys.
        if isinstance(libraries, dict) and "Items" in libraries:
            libraries = [
                {"ItemId": lib["Id"], "Name": lib["Name"], "CollectionType": lib.get("CollectionType")}
                for lib in libraries["Items"]
                if lib.get("Id") and lib.get("Name")
            ]

        return libraries if libraries else None

    async def _fetch_libraries_from_endpoint(self, endpoint, auth_attempts, _sanitize_headers):
        """
        Attempts to fetch libraries from the given endpoint using multiple auth methods.
        Returns the parsed JSON response on success, or None on failure.
        """
        url = f"{self.api_url}{endpoint}"
        last_failure = None

        try:
            session = await self._get_session()
            first_attempt_unauthorized = False

            for index, attempt in enumerate(auth_attempts):
                headers = attempt["headers"]
                params = attempt["params"]

                self.logger.debug(
                    "Jellyfin request prepared: base_url=%s endpoint=%s url=%s headers=%s token_length=%d",
                    self.api_url,
                    endpoint,
                    url,
                    _sanitize_headers(headers),
                    len(self.api_token) if self.api_token else 0,
                )

                if index == 1:
                    self.logger.debug("Retrying Jellyfin request using MediaBrowser Authorization header")
                elif index == 2:
                    self.logger.debug("Retrying Jellyfin request using api_key query parameter")

                try:
                    request_cm = session.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=self.REQUEST_TIMEOUT,
                    )
                except StopIteration:
                    self.logger.error(
                        "Unexpected termination while retrieving libraries from %s",
                        endpoint,
                    )
                    return None

                async with request_cm as response:
                    if response.status == 200:
                        libraries = await response.json()
                        self.logger.debug(
                            "Jellyfin libraries request succeeded using auth method: %s (endpoint: %s)",
                            attempt["name"],
                            endpoint,
                        )
                        self.logger.debug(f'Libraries retrieved: {libraries}')
                        return libraries

                    raw_body = await response.text()
                    last_failure = {
                        "status": response.status,
                        "headers": response.headers,
                        "body": raw_body,
                        "method": attempt["name"],
                    }

                    if index == 0 and response.status == 401:
                        first_attempt_unauthorized = True
                        self.logger.warning(
                            "Jellyfin libraries request unauthorized with %s. "
                            "Will retry with alternate authentication methods.",
                            attempt["name"],
                        )
                        continue

                    if index == 0 and response.status != 401:
                        self.logger.error(
                            "Failed to get libraries %d URL=%s body=%.300s",
                            response.status,
                            url,
                            raw_body,
                        )
                        return None

            if first_attempt_unauthorized and last_failure is not None:
                self.logger.error(
                    "Jellyfin auth failed after all methods. status=%s headers=%s body=%s",
                    last_failure["status"],
                    last_failure["headers"],
                    last_failure["body"],
                )
            elif last_failure is not None:
                self.logger.error(
                    "Failed to get libraries %s URL=%s body=%.300s",
                    last_failure["status"],
                    url,
                    last_failure["body"],
                )
        except StopIteration:
            self.logger.error(
                "Unexpected termination while retrieving libraries from %s",
                endpoint,
            )
        except RuntimeError as e:
            if isinstance(e.__cause__, StopIteration):
                self.logger.error(
                    "Unexpected termination while retrieving libraries from %s",
                    endpoint,
                )
            else:
                raise
        except aiohttp.ClientError as e:
            self.logger.error(
                "An error occurred while retrieving libraries: %s", str(e))

        return None
