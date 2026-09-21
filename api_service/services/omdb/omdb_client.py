"""
OMDb API client for fetching IMDB ratings.

The OMDb API (Open Movie Database) provides IMDB rating data
using IMDB IDs (tt... format).
"""

import aiohttp
from api_service.services.http.base_client import BaseHTTPClient
from api_service.config.logger_manager import LoggerManager

HTTP_OK = {200, 201}


class OmdbClient(BaseHTTPClient):
    """
    Client for interacting with the OMDb API to retrieve IMDB ratings.

    Uses the free OMDb API (https://www.omdbapi.com/) which returns
    IMDB ratings, vote counts, and other metadata by IMDB ID.
    """

    def __init__(self, api_key):
        """
        Initialize the OmdbClient.

        Args:
            api_key (str): OMDb API key (free tier at omdbapi.com).
        """
        super().__init__()
        self.api_key = api_key
        self.base_url = "https://www.omdbapi.com/"
        self.logger.debug("OmdbClient initialized")

    async def get_rating(self, imdb_id):
        """
        Fetch IMDB rating and vote count for a given IMDB ID.

        Args:
            imdb_id (str): IMDB ID in tt... format (e.g., 'tt0816692').

        Returns:
            dict | None: Dictionary with 'imdb_rating' (float) and
                         'imdb_votes' (int), or None if unavailable or
                         the request fails.
        """
        if not imdb_id or not self.api_key:
            return None

        url = f"{self.base_url}?i={imdb_id}&apikey={self.api_key}"
        self.logger.debug("Fetching OMDb rating for IMDB ID %s", imdb_id)

        try:
            session = await self._get_session()
            async with session.get(url, timeout=self.REQUEST_TIMEOUT) as response:
                if response.status in HTTP_OK:
                    data = await response.json()

                    if data.get('Response') == 'False':
                        self.logger.debug("OMDb returned no result for IMDB ID %s: %s",
                                          imdb_id, data.get('Error'))
                        return None

                    raw_rating = data.get('imdbRating')
                    raw_votes = data.get('imdbVotes')

                    rating_missing = raw_rating in (None, '', 'N/A')
                    votes_missing = raw_votes in (None, '', 'N/A')

                    imdb_votes = None
                    if not votes_missing:
                        try:
                            imdb_votes = int(str(raw_votes).replace(',', ''))
                        except (ValueError, TypeError) as e:
                            self.logger.warning(
                                "Failed to parse IMDB votes for %s: %s", imdb_id, str(e)
                            )

                    if rating_missing:
                        self.logger.debug(
                            "No valid IMDB rating for IMDB ID %s (imdbRating=%s, imdbVotes=%s)",
                            imdb_id,
                            raw_rating,
                            raw_votes,
                        )
                        return {
                            'imdb_rating': None,
                            'imdb_votes': imdb_votes,
                            'imdb_rating_raw': raw_rating,
                        }

                    try:
                        imdb_rating = float(raw_rating)
                        self.logger.debug(
                            "IMDB rating for %s: %.1f (%s votes)",
                            imdb_id,
                            imdb_rating,
                            imdb_votes if imdb_votes is not None else 'unknown',
                        )
                        return {
                            'imdb_rating': imdb_rating,
                            'imdb_votes': imdb_votes,
                            'imdb_rating_raw': raw_rating,
                        }
                    except (ValueError, TypeError) as e:
                        self.logger.warning(
                            "Failed to parse IMDB rating data for %s: %s", imdb_id, str(e)
                        )
                        return {
                            'imdb_rating': None,
                            'imdb_votes': imdb_votes,
                            'imdb_rating_raw': raw_rating,
                        }
                else:
                    self.logger.warning("OMDb request failed for IMDB ID %s: HTTP %d",
                                        imdb_id, response.status)
        except aiohttp.ClientError as e:
            self.logger.error("OMDb request error for IMDB ID %s: %s", imdb_id, str(e))

        return None

    async def get_ratings(self, imdb_id):
        """
        Fetch every rating OMDb aggregates for a title, for display.

        Args:
            imdb_id (str): IMDB ID in tt... format.

        Returns:
            dict | None: {'imdb_rating': float|None, 'imdb_votes': int|None,
                          'rotten_tomatoes': int|None (percent),
                          'metascore': int|None}, or None if OMDb has no entry
                          or the request fails.
        """
        if not imdb_id or not self.api_key:
            return None

        url = f"{self.base_url}?i={imdb_id}&apikey={self.api_key}"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=self.REQUEST_TIMEOUT) as response:
                if response.status not in HTTP_OK:
                    self.logger.warning("OMDb request failed for IMDB ID %s: HTTP %d",
                                        imdb_id, response.status)
                    return None
                data = await response.json()
        except aiohttp.ClientError as e:
            self.logger.error("OMDb request error for IMDB ID %s: %s", imdb_id, str(e))
            return None

        if data.get('Response') == 'False':
            return None

        rotten_tomatoes = None
        for rating in data.get('Ratings') or []:
            if rating.get('Source') == 'Rotten Tomatoes':
                rotten_tomatoes = self._parse_number(str(rating.get('Value', '')).rstrip('%'), int)
        return {
            'imdb_rating': self._parse_number(data.get('imdbRating'), float),
            'imdb_votes': self._parse_number(str(data.get('imdbVotes', '')).replace(',', ''), int),
            'rotten_tomatoes': rotten_tomatoes,
            'metascore': self._parse_number(data.get('Metascore'), int),
        }

    @staticmethod
    def _parse_number(raw, cast):
        """Convert an OMDb field ('N/A', '', '7.8', '1,234') to a number or None."""
        if raw in (None, '', 'N/A'):
            return None
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return None
