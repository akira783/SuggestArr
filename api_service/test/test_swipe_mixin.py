import logging
import sqlite3

import pytest

from api_service.db.components.swipe_mixin import SwipeMixin
from api_service.db.components.schema_manager import SchemaManager


class SwipeDb(SwipeMixin):
    """In-memory SQLite database built from the real SchemaManager DDL."""

    db_type = 'sqlite'

    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.logger = logging.getLogger('test_swipe_mixin')

    def get_connection(self):
        return self.connection


@pytest.fixture
def db():
    database = SwipeDb()
    SchemaManager(database).initialize_db()
    database.connection.execute("PRAGMA foreign_keys = ON")
    for user_id, username in ((1, 'akira'), (2, 'guest')):
        database.connection.execute(
            "INSERT INTO auth_users (id, username, password_hash) VALUES (?, ?, 'x')",
            (user_id, username),
        )
    database.connection.commit()
    return database


def _vote(db, user_id, tmdb_id, vote='like', media_type='movie', **extra):
    return db.set_swipe_vote(user_id, tmdb_id, media_type, vote, **extra)


def test_vote_round_trip_keeps_card_metadata(db):
    stored = _vote(db, 1, 438631, title='Dune', year=2021, genres=['Science Fiction', 'Adventure'],
                   rationale='Because you loved Blade Runner 2049', pick_type='safe')

    assert stored['tmdb_id'] == '438631'
    [vote] = db.get_swipe_votes(1)
    assert vote['tmdb_id'] == '438631'
    assert vote['media_type'] == 'movie'
    assert vote['vote'] == 'like'
    assert vote['title'] == 'Dune'
    assert vote['year'] == 2021
    assert vote['genres'] == ['Science Fiction', 'Adventure']
    assert vote['rationale'] == 'Because you loved Blade Runner 2049'
    assert vote['pick_type'] == 'safe'
    assert vote['requested'] is False
    assert vote['created_at'] is not None


def test_revote_replaces_vote_but_keeps_requested_flag(db):
    _vote(db, 1, 42, 'like')
    assert db.mark_swipe_requested(1, 42, 'movie') is True

    _vote(db, 1, 42, 'seen_liked')

    [vote] = db.get_swipe_votes(1)
    assert vote['vote'] == 'seen_liked'
    assert vote['requested'] is True


def test_revote_without_metadata_keeps_stored_card_details(db):
    _vote(db, 1, 42, 'like', title='Dune', year=2021, genres=['Science Fiction'],
          rationale='Because you loved Blade Runner 2049', pick_type='explore')

    _vote(db, 1, 42, 'seen_disliked')

    [vote] = db.get_swipe_votes(1)
    assert vote['vote'] == 'seen_disliked'
    assert vote['title'] == 'Dune'
    assert vote['year'] == 2021
    assert vote['genres'] == ['Science Fiction']
    assert vote['rationale'] == 'Because you loved Blade Runner 2049'
    assert vote['pick_type'] == 'explore'


def test_same_tmdb_id_is_distinct_per_media_type(db):
    _vote(db, 1, 1399, 'like', media_type='tv')
    _vote(db, 1, 1399, 'dislike', media_type='movie')

    assert db.get_swipe_voted_ids(1) == {('1399', 'tv'), ('1399', 'movie')}
    assert db.get_swipe_voted_ids(1, media_type='tv') == {('1399', 'tv')}


def test_votes_are_isolated_between_users(db):
    _vote(db, 1, 1, 'like')
    _vote(db, 1, 2, 'dislike')
    _vote(db, 2, 3, 'like')

    assert db.get_swipe_voted_ids(1) == {('1', 'movie'), ('2', 'movie')}
    assert db.get_swipe_voted_ids(2) == {('3', 'movie')}
    assert db.count_swipe_votes(1) == 2
    assert db.count_swipe_votes(2) == 1
    assert db.mark_swipe_requested(2, 1, 'movie') is False


def test_get_votes_orders_newest_first_and_honours_limit(db):
    for tmdb_id in (10, 20, 30):
        _vote(db, 1, tmdb_id)
    # Same-second writes: make the order explicit rather than relying on clock resolution.
    for tmdb_id, stamp in ((10, '2026-09-21 10:00:00'), (20, '2026-09-21 12:00:00'),
                           (30, '2026-09-21 11:00:00')):
        db.connection.execute(
            "UPDATE swipe_votes SET updated_at=? WHERE tmdb_id=?", (stamp, str(tmdb_id)),
        )

    assert [v['tmdb_id'] for v in db.get_swipe_votes(1)] == ['20', '30', '10']
    assert [v['tmdb_id'] for v in db.get_swipe_votes(1, limit=2)] == ['20', '30']


def test_stats_compute_like_and_request_rates(db):
    _vote(db, 1, 1, 'like', pick_type='safe')
    _vote(db, 1, 2, 'like', pick_type='explore')
    _vote(db, 1, 3, 'dislike', pick_type='explore')
    _vote(db, 1, 4, 'seen_liked', pick_type='calibration')
    _vote(db, 1, 5, 'seen_disliked', pick_type='calibration')
    db.mark_swipe_requested(1, 1, 'movie')

    stats = db.get_swipe_stats(1)

    assert stats['total'] == 5
    assert stats['likes'] == 3
    assert stats['dislikes'] == 2
    assert stats['requested'] == 1
    assert stats['like_rate'] == 0.6
    # Only card likes can become requests: 1 requested out of 2 'like' votes.
    assert stats['request_rate'] == 0.5
    assert stats['by_pick_type']['explore'] == {'total': 2, 'likes': 1}
    assert stats['by_pick_type']['calibration'] == {'total': 2, 'likes': 1}


def test_stats_for_user_without_votes(db):
    assert db.get_swipe_stats(1) == {
        'total': 0, 'likes': 0, 'dislikes': 0, 'requested': 0,
        'like_rate': 0.0, 'request_rate': 0.0, 'by_pick_type': {},
    }


@pytest.mark.parametrize('kwargs, message', [
    ({'vote': 'love'}, 'vote must be one of'),
    ({'media_type': 'music'}, "media_type must be 'movie' or 'tv'"),
    ({'pick_type': 'random'}, 'pick_type must be one of'),
])
def test_invalid_vote_values_are_rejected(db, kwargs, message):
    args = {'vote': 'like', 'media_type': 'movie'}
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        db.set_swipe_vote(1, 1, args.pop('media_type'), args.pop('vote'), **args)
    assert db.count_swipe_votes(1) == 0


def test_taste_profile_absent_until_saved(db):
    assert db.get_taste_profile(1) is None


def test_votes_before_first_profile_are_counted(db):
    assert db.increment_taste_profile_votes(1) == 1
    assert db.increment_taste_profile_votes(1) == 2
    # The placeholder row has no text, so there is still no profile to show.
    assert db.get_taste_profile(1) is None

    db.save_taste_profile(1, 'Loves thoughtful sci-fi.')

    profile = db.get_taste_profile(1)
    assert profile['profile_text'] == 'Loves thoughtful sci-fi.'
    assert profile['votes_since_update'] == 0
    assert profile['user_edited'] is False


def test_saving_profile_resets_counter_and_tracks_manual_edit(db):
    db.save_taste_profile(1, 'Generated profile.')
    for _ in range(3):
        db.increment_taste_profile_votes(1)
    assert db.get_taste_profile(1)['votes_since_update'] == 3

    db.save_taste_profile(1, '  No superheroes, please.  ', user_edited=True)

    profile = db.get_taste_profile(1)
    assert profile['profile_text'] == 'No superheroes, please.'
    assert profile['votes_since_update'] == 0
    assert profile['user_edited'] is True


def test_empty_profile_text_is_rejected(db):
    with pytest.raises(ValueError, match='profile_text must not be empty'):
        db.save_taste_profile(1, '   ')


def test_profiles_are_isolated_between_users(db):
    db.save_taste_profile(1, 'Profile of user 1.')
    db.increment_taste_profile_votes(2)

    assert db.get_taste_profile(1)['profile_text'] == 'Profile of user 1.'
    assert db.get_taste_profile(2) is None


def test_clear_votes_resets_counter_but_keeps_profile(db):
    db.save_taste_profile(1, 'Keep me.')
    _vote(db, 1, 1)
    _vote(db, 1, 2)
    db.increment_taste_profile_votes(1)
    _vote(db, 2, 3)

    assert db.clear_swipe_votes(1) == 2

    assert db.count_swipe_votes(1) == 0
    assert db.count_swipe_votes(2) == 1
    profile = db.get_taste_profile(1)
    assert profile['profile_text'] == 'Keep me.'
    assert profile['votes_since_update'] == 0


def test_deleting_a_user_cascades_to_swipe_rows(db):
    _vote(db, 1, 1)
    db.save_taste_profile(1, 'Gone soon.')

    db.connection.execute("DELETE FROM auth_users WHERE id=1")

    assert db.count_swipe_votes(1) == 0
    assert db.get_taste_profile(1) is None


def test_mysql_ddl_keeps_long_text_columns():
    manager = SchemaManager(None)

    votes = manager._prepare_create_table_query_for_db('swipe_votes', '', 'mysql')
    profile = manager._prepare_create_table_query_for_db('swipe_taste_profile', '', 'mysql')

    # The generic MySQL rewrite maps TEXT to VARCHAR(512); these columns must escape it.
    assert 'rationale TEXT' in votes
    assert 'genres TEXT' in votes
    assert 'FOREIGN KEY (user_id) REFERENCES auth_users(id) ON DELETE CASCADE' in votes
    assert 'profile_text TEXT NOT NULL' in profile
    assert votes.strip().endswith('ENGINE=InnoDB')
    assert profile.strip().endswith('ENGINE=InnoDB')


def test_preferences_track_last_media_type_and_activity(db):
    db.set_swipe_preference(1, 'tv')
    db.set_swipe_preference(1, 'movie')
    db.set_swipe_preference(2, 'both')
    db.connection.execute(
        "UPDATE swipe_preferences SET updated_at='2020-01-01 00:00:00' WHERE user_id=2")

    assert db.get_swipe_active_users(14) == [(1, 'movie', 'balanced')]
    assert sorted(db.get_swipe_active_users(100000)) == [(1, 'movie', 'balanced'),
                                                         (2, 'both', 'balanced')]
    with pytest.raises(ValueError):
        db.set_swipe_preference(1, 'music')


def test_poster_and_vote_filters(db):
    _vote(db, 1, 1, 'like', poster_path='https://image.tmdb.org/t/p/w500/a.jpg')
    _vote(db, 1, 2, 'like')
    _vote(db, 1, 3, 'seen_liked')
    db.mark_swipe_requested(1, 2, 'movie')
    # A re-vote without the poster keeps it.
    _vote(db, 1, 1, 'like')

    likes = db.get_swipe_votes(1, votes=('like',))
    assert {v['tmdb_id'] for v in likes} == {'1', '2'}
    assert [v['tmdb_id'] for v in db.get_swipe_votes(1, votes=('like',), requested=False)] == ['1']
    assert db.get_swipe_votes(1, votes=('like',), requested=False)[0]['poster_path'].endswith('/a.jpg')
    assert [v['tmdb_id'] for v in db.get_swipe_votes(1, votes=('like',), requested=True)] == ['2']


def test_preferences_store_novelty(db):
    db.set_swipe_preference(1, 'tv', 'bold')
    assert db.get_swipe_active_users() == [(1, 'tv', 'bold')]
    with pytest.raises(ValueError):
        db.set_swipe_preference(1, 'tv', 'wild')


def test_migration_adds_columns_to_tables_created_before():
    database = SwipeDb()
    database.connection.executescript("""
        CREATE TABLE swipe_votes (user_id INTEGER NOT NULL, tmdb_id TEXT NOT NULL,
            media_type TEXT NOT NULL, vote TEXT NOT NULL, title TEXT, year INTEGER, genres TEXT,
            rationale TEXT, pick_type TEXT, requested INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, tmdb_id, media_type));
        CREATE TABLE swipe_preferences (user_id INTEGER PRIMARY KEY,
            media_type TEXT NOT NULL DEFAULT 'both', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO swipe_preferences (user_id, media_type) VALUES (1, 'tv');
    """)
    SchemaManager(database).initialize_db()

    votes_columns = {row[1] for row in database.connection.execute('PRAGMA table_info(swipe_votes)')}
    assert 'poster_path' in votes_columns
    assert database.get_swipe_active_users(100000) == [(1, 'tv', 'balanced')]
