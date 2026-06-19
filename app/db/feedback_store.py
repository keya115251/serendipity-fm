"""
Per-user feedback storage and retrieval for the feedback loop feature.

Sits alongside ArtistDataCache (app/db/artist_cache.py) but is
conceptually distinct: ArtistDataCache holds shared, global Last.fm data
the same for every user; this module holds per-user state keyed by
Last.fm username, recording explicit like/dislike feedback on
recommended artists and albums and exposing it in the shapes the rest of
the pipeline actually needs.

Effects on future recommendations (see also DiscoveryWalk and
album_selection.py for where these get consumed):
  - disliked ARTIST: excluded entirely (folded into known_artists at
    walk time, the same as an artist the user already listens to)
  - liked ARTIST: small positive boost, not a structural change
  - disliked ALBUM: down-weighted in entry-point selection, NOT a full
    exclusion - disliking one album shouldn't blacklist the whole artist
  - liked ALBUM: small positive boost
"""

from sqlalchemy.exc import IntegrityError

from app.db.models import UserFeedback, get_session_factory


class FeedbackStore:
    def __init__(self, database_path: str):
        self.SessionFactory = get_session_factory(database_path)

    def record_artist_feedback(self, username: str, artist_name: str, sentiment: str) -> None:
        """sentiment must be 'liked' or 'disliked'. Upserts - a repeat vote on the same artist replaces the prior one."""
        self._upsert(username, "artist", artist_name, "", sentiment)

    def record_album_feedback(self, username: str, artist_name: str, album_name: str, sentiment: str) -> None:
        """sentiment must be 'liked' or 'disliked'. Upserts - a repeat vote on the same album replaces the prior one."""
        self._upsert(username, "album", artist_name, album_name, sentiment)

    def _upsert(self, username: str, target_type: str, artist_name: str, album_name: str, sentiment: str) -> None:
        if sentiment not in ("liked", "disliked"):
            raise ValueError(f"sentiment must be 'liked' or 'disliked', got {sentiment!r}")

        session = self.SessionFactory()
        try:
            existing = (
                session.query(UserFeedback)
                .filter_by(username=username, target_type=target_type, artist_name=artist_name, album_name=album_name)
                .first()
            )
            if existing:
                existing.sentiment = sentiment
            else:
                session.add(
                    UserFeedback(
                        username=username,
                        target_type=target_type,
                        artist_name=artist_name,
                        album_name=album_name,
                        sentiment=sentiment,
                    )
                )
            session.commit()
        except IntegrityError:
            # race between the existence check and insert (e.g. two
            # near-simultaneous requests for the same vote) - safe to
            # ignore, the row exists either way, which is the goal
            session.rollback()
        finally:
            session.close()

    def get_disliked_artists(self, username: str) -> set[str]:
        """Returns the set of artist names this user has disliked - meant to be folded into known_artists at walk time, so the walk never recommends them again."""
        session = self.SessionFactory()
        try:
            rows = (
                session.query(UserFeedback.artist_name)
                .filter_by(username=username, target_type="artist", sentiment="disliked")
                .all()
            )
            return {r.artist_name for r in rows}
        finally:
            session.close()

    def get_liked_artists(self, username: str) -> set[str]:
        """Returns the set of artist names this user has liked, for a small positive scoring boost."""
        session = self.SessionFactory()
        try:
            rows = (
                session.query(UserFeedback.artist_name)
                .filter_by(username=username, target_type="artist", sentiment="liked")
                .all()
            )
            return {r.artist_name for r in rows}
        finally:
            session.close()

    def get_album_feedback(self, username: str) -> dict[tuple[str, str], str]:
        """
        Returns a dict mapping (artist_name, album_name) -> "liked" or
        "disliked" for every album this user has voted on - meant to be
        looked up during entry-point album selection to down-weight a
        disliked pick or boost a liked one, without excluding the artist
        entirely.
        """
        session = self.SessionFactory()
        try:
            rows = session.query(UserFeedback).filter_by(username=username, target_type="album").all()
            return {(r.artist_name, r.album_name): r.sentiment for r in rows}
        finally:
            session.close()
