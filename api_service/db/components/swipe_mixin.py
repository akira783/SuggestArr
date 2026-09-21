"""Per-user storage for the Swipe feature: card votes and the AI taste profile.

Unlike the AI Search tables (``ai_search_feedback``, ``ai_search_seen``), every row
here belongs to one SuggestArr account (``auth_users.id``), so two people sharing an
instance never see or influence each other's cards.
"""

import json


class SwipeMixin:
    SWIPE_VOTES = ('like', 'dislike', 'seen_liked', 'seen_disliked')
    SWIPE_POSITIVE_VOTES = ('like', 'seen_liked')
    SWIPE_PICK_TYPES = ('safe', 'explore', 'calibration')
    SWIPE_MEDIA_TYPES = ('movie', 'tv')

    def _swipe_placeholder(self):
        return '%s' if self.db_type in ('mysql', 'mariadb', 'postgres') else '?'

    @staticmethod
    def _swipe_timestamp(value):
        return None if value is None else str(value)

    def _validate_swipe_media_type(self, media_type):
        if media_type not in self.SWIPE_MEDIA_TYPES:
            raise ValueError("media_type must be 'movie' or 'tv'")

    def set_swipe_vote(self, user_id, tmdb_id, media_type, vote, title=None, year=None,
                          genres=None, rationale=None, pick_type=None):
        """Insert or update one user's vote on a Swipe card.

        Re-voting on the same title replaces the vote but keeps the ``requested`` flag,
        the original ``created_at``, and any stored card metadata the new call leaves
        as None (e.g. "Already seen" sent without the card details).

        :param user_id: SuggestArr account id (``auth_users.id``).
        :param tmdb_id: TMDb id of the title.
        :param media_type: 'movie' or 'tv'.
        :param vote: One of ``SWIPE_VOTES``.
        :param title: Title shown on the card, kept for LLM prompts.
        :param year: Release year, kept for LLM prompts.
        :param genres: List of genre names, stored as JSON.
        :param rationale: The AI's "why for you" text shown on the card.
        :param pick_type: One of ``SWIPE_PICK_TYPES`` or None.
        :return: The vote as sent by the caller (stored metadata may be richer, see above).
        :raises ValueError: On an unknown vote, media type or pick type.
        """
        if vote not in self.SWIPE_VOTES:
            raise ValueError(f"vote must be one of {', '.join(self.SWIPE_VOTES)}")
        self._validate_swipe_media_type(media_type)
        if pick_type is not None and pick_type not in self.SWIPE_PICK_TYPES:
            raise ValueError(f"pick_type must be one of {', '.join(self.SWIPE_PICK_TYPES)}")

        ph = self._swipe_placeholder()
        genres_json = json.dumps(list(genres)) if genres else None
        params = (int(user_id), str(tmdb_id), media_type, vote, title, year, genres_json,
                  rationale, pick_type)
        columns = "(user_id, tmdb_id, media_type, vote, title, year, genres, rationale, pick_type)"
        values = f"VALUES ({', '.join([ph] * len(params))})"
        if self.db_type in ('mysql', 'mariadb'):
            query = f"""
                INSERT INTO swipe_votes {columns} {values}
                ON DUPLICATE KEY UPDATE vote=VALUES(vote),
                    title=COALESCE(VALUES(title), title), year=COALESCE(VALUES(year), year),
                    genres=COALESCE(VALUES(genres), genres),
                    rationale=COALESCE(VALUES(rationale), rationale),
                    pick_type=COALESCE(VALUES(pick_type), pick_type), updated_at=CURRENT_TIMESTAMP
            """
        else:
            query = f"""
                INSERT INTO swipe_votes {columns} {values}
                ON CONFLICT(user_id, tmdb_id, media_type) DO UPDATE SET
                    vote=excluded.vote,
                    title=COALESCE(excluded.title, swipe_votes.title),
                    year=COALESCE(excluded.year, swipe_votes.year),
                    genres=COALESCE(excluded.genres, swipe_votes.genres),
                    rationale=COALESCE(excluded.rationale, swipe_votes.rationale),
                    pick_type=COALESCE(excluded.pick_type, swipe_votes.pick_type),
                    updated_at=CURRENT_TIMESTAMP
            """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
        return {
            'tmdb_id': str(tmdb_id),
            'media_type': media_type,
            'vote': vote,
            'title': title,
            'year': year,
            'genres': list(genres) if genres else [],
            'rationale': rationale,
            'pick_type': pick_type,
        }

    def mark_swipe_requested(self, user_id, tmdb_id, media_type):
        """Flag a voted card as requested.

        :return: True if a vote existed and was flagged, False otherwise.
        """
        self._validate_swipe_media_type(media_type)
        ph = self._swipe_placeholder()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE swipe_votes SET requested=1, updated_at=CURRENT_TIMESTAMP "
                f"WHERE user_id={ph} AND tmdb_id={ph} AND media_type={ph}",
                (int(user_id), str(tmdb_id), media_type),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_swipe_votes(self, user_id, limit=None, media_type=None):
        """Return a user's votes, most recently changed first.

        :param limit: Maximum number of rows, or None for all.
        :param media_type: Restrict to 'movie' or 'tv', or None for both.
        :return: List of vote dicts.
        """
        ph = self._swipe_placeholder()
        query = (
            "SELECT tmdb_id, media_type, vote, title, year, genres, rationale, pick_type, "
            "requested, created_at, updated_at FROM swipe_votes WHERE user_id=" + ph
        )
        params = [int(user_id)]
        if media_type is not None:
            self._validate_swipe_media_type(media_type)
            query += f" AND media_type={ph}"
            params.append(media_type)
        # updated_at has one-second resolution; created_at/tmdb_id keep the order stable.
        query += " ORDER BY updated_at DESC, created_at DESC, tmdb_id DESC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
        votes = []
        for row in rows:
            try:
                genres = json.loads(row[5]) if row[5] else []
            except (TypeError, json.JSONDecodeError):
                genres = []
            votes.append({
                'tmdb_id': str(row[0]),
                'media_type': row[1],
                'vote': row[2],
                'title': row[3],
                'year': row[4],
                'genres': genres,
                'rationale': row[6],
                'pick_type': row[7],
                'requested': bool(row[8]),
                'created_at': self._swipe_timestamp(row[9]),
                'updated_at': self._swipe_timestamp(row[10]),
            })
        return votes

    def get_swipe_voted_ids(self, user_id, media_type=None):
        """Return the TMDb ids a user already voted on, to exclude them from new cards.

        :return: Set of ``(tmdb_id, media_type)`` tuples.
        """
        ph = self._swipe_placeholder()
        query = f"SELECT tmdb_id, media_type FROM swipe_votes WHERE user_id={ph}"
        params = [int(user_id)]
        if media_type is not None:
            self._validate_swipe_media_type(media_type)
            query += f" AND media_type={ph}"
            params.append(media_type)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, tuple(params))
            return {(str(row[0]), row[1]) for row in cursor.fetchall()}

    def count_swipe_votes(self, user_id):
        """Return how many cards a user has voted on (drives the Calibration mode)."""
        ph = self._swipe_placeholder()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM swipe_votes WHERE user_id={ph}", (int(user_id),))
            return int(cursor.fetchone()[0])

    def get_swipe_stats(self, user_id):
        """Return the success indicators from the spec, computed from stored votes.

        :return: Dict with ``total``, ``likes`` (like + seen_liked), ``dislikes``,
            ``requested``, ``like_rate`` and ``request_rate`` (requested / like), plus
            ``by_pick_type`` counts of positive votes and totals.
        """
        ph = self._swipe_placeholder()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT vote, pick_type, requested FROM swipe_votes WHERE user_id={ph}",
                (int(user_id),),
            )
            rows = cursor.fetchall()
        total = len(rows)
        likes = sum(1 for vote, _, _ in rows if vote in self.SWIPE_POSITIVE_VOTES)
        card_likes = sum(1 for vote, _, _ in rows if vote == 'like')
        requested = sum(1 for _, _, req in rows if req)
        by_pick_type = {}
        for vote, pick_type, _ in rows:
            bucket = by_pick_type.setdefault(pick_type or 'unknown', {'total': 0, 'likes': 0})
            bucket['total'] += 1
            if vote in self.SWIPE_POSITIVE_VOTES:
                bucket['likes'] += 1
        return {
            'total': total,
            'likes': likes,
            'dislikes': total - likes,
            'requested': requested,
            'like_rate': round(likes / total, 3) if total else 0.0,
            'request_rate': round(requested / card_likes, 3) if card_likes else 0.0,
            'by_pick_type': by_pick_type,
        }

    def clear_swipe_votes(self, user_id):
        """Delete all of a user's votes and reset their profile update counter.

        The taste profile text itself is kept; regenerate it separately if wanted.

        :return: Number of deleted votes.
        """
        ph = self._swipe_placeholder()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM swipe_votes WHERE user_id={ph}", (int(user_id),))
            deleted = cursor.rowcount
            cursor.execute(
                f"UPDATE swipe_taste_profile SET votes_since_update=0 WHERE user_id={ph}",
                (int(user_id),),
            )
            conn.commit()
        return deleted

    def get_taste_profile(self, user_id):
        """Return a user's taste profile, or None if no profile text exists yet.

        :return: Dict with ``profile_text``, ``votes_since_update``, ``user_edited`` and
            ``updated_at``, or None.
        """
        ph = self._swipe_placeholder()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT profile_text, votes_since_update, user_edited, updated_at "
                f"FROM swipe_taste_profile WHERE user_id={ph}",
                (int(user_id),),
            )
            row = cursor.fetchone()
        if not row or not (row[0] or '').strip():
            return None
        return {
            'profile_text': row[0],
            'votes_since_update': int(row[1] or 0),
            'user_edited': bool(row[2]),
            'updated_at': self._swipe_timestamp(row[3]),
        }

    def save_taste_profile(self, user_id, profile_text, user_edited=False):
        """Store a new profile text and reset the vote counter.

        :param profile_text: The profile, written by the LLM or edited by the user.
        :param user_edited: True when the user wrote or corrected the text by hand. An
            LLM refresh passes False; the service decides whether to keep the flag.
        :raises ValueError: If ``profile_text`` is empty.
        """
        profile_text = (profile_text or '').strip()
        if not profile_text:
            raise ValueError("profile_text must not be empty")
        ph = self._swipe_placeholder()
        params = (int(user_id), profile_text, 1 if user_edited else 0)
        if self.db_type in ('mysql', 'mariadb'):
            query = f"""
                INSERT INTO swipe_taste_profile (user_id, profile_text, votes_since_update, user_edited)
                VALUES ({ph}, {ph}, 0, {ph})
                ON DUPLICATE KEY UPDATE profile_text=VALUES(profile_text), votes_since_update=0,
                    user_edited=VALUES(user_edited), updated_at=CURRENT_TIMESTAMP
            """
        else:
            query = f"""
                INSERT INTO swipe_taste_profile (user_id, profile_text, votes_since_update, user_edited)
                VALUES ({ph}, {ph}, 0, {ph})
                ON CONFLICT(user_id) DO UPDATE SET profile_text=excluded.profile_text,
                    votes_since_update=0, user_edited=excluded.user_edited,
                    updated_at=CURRENT_TIMESTAMP
            """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()

    def increment_taste_profile_votes(self, user_id):
        """Count one more vote towards the next profile refresh.

        Creates an empty placeholder row when the user has no profile yet, so votes cast
        before the first profile is generated are not lost.

        :return: The new ``votes_since_update`` value.
        """
        ph = self._swipe_placeholder()
        if self.db_type in ('mysql', 'mariadb'):
            upsert = f"""
                INSERT INTO swipe_taste_profile (user_id, profile_text, votes_since_update)
                VALUES ({ph}, '', 1)
                ON DUPLICATE KEY UPDATE votes_since_update=votes_since_update + 1
            """
        else:
            upsert = f"""
                INSERT INTO swipe_taste_profile (user_id, profile_text, votes_since_update)
                VALUES ({ph}, '', 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    votes_since_update=swipe_taste_profile.votes_since_update + 1
            """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(upsert, (int(user_id),))
            cursor.execute(
                f"SELECT votes_since_update FROM swipe_taste_profile WHERE user_id={ph}",
                (int(user_id),),
            )
            count = int(cursor.fetchone()[0])
            conn.commit()
        return count
