"""
Persistent cache schema for Last.fm data that's expensive to refetch and
slow-changing: similarity edges, artist info (listener/playcount), and
tags.

This exists because hop-expansion for the discovery walk fans out fast -
a single seed artist with similar_limit=30 produces up to 900 second-hop
candidates, each potentially needing its own getSimilar, getInfo, and
getTopTags calls. Re-fetching all of that live on every request isn't
viable, and most of this data changes slowly enough that a cache with a
multi-day refresh window is a reasonable tradeoff, not a staleness risk.

SQLite is intentionally used here rather than a heavier DB - this is a
read-heavy, single-process cache for a portfolio-scale deployment, not a
system that needs concurrent-write guarantees at scale.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ArtistInfo(Base):
    """
    Cached artist.getInfo result - listener/playcount for popularity
    scoring, plus mbid (MusicBrainz ID). mbid is nullable: a NULL value is
    itself meaningful, not missing data - it indicates a likely
    collaboration/compilation credit rather than a real catalogued artist
    (see LastFMClient.get_artist_info docstring for the real test case
    this was validated against).
    """

    __tablename__ = "artist_info"

    artist_name = Column(String, primary_key=True)
    listeners = Column(Integer, nullable=False, default=0)
    playcount = Column(Integer, nullable=False, default=0)
    mbid = Column(String, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class ArtistTag(Base):
    """Cached artist.getTopTags result - one row per (artist, tag) pair."""

    __tablename__ = "artist_tags"
    __table_args__ = (UniqueConstraint("artist_name", "tag", name="uq_artist_tag"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    artist_name = Column(String, nullable=False, index=True)
    tag = Column(String, nullable=False)
    weight = Column(Integer, nullable=False)  # Last.fm's 0-100 relative weight
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class SimilarityEdge(Base):
    """
    Cached artist.getSimilar result - one row per (source_artist, similar_artist)
    pair. Stored directed as Last.fm returns it (A's similar list includes B
    doesn't guarantee B's similar list includes A), so the walk logic should
    treat this as a directed graph even though most edges end up reciprocal
    in practice.
    """

    __tablename__ = "similarity_edges"
    __table_args__ = (UniqueConstraint("source_artist", "similar_artist", name="uq_similarity_edge"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_artist = Column(String, nullable=False, index=True)
    similar_artist = Column(String, nullable=False)
    match = Column(Float, nullable=False)  # Last.fm's 0-1 similarity score
    fetched_at = Column(DateTime, nullable=False, default=utcnow)


class UserFeedback(Base):
    """
    Per-user like/dislike feedback on a recommended artist or album.

    Unlike every other table in this file (which cache shared, global
    Last.fm data the same for every user), this table is genuinely
    per-user state, keyed by Last.fm username. There's no account system
    in this project, so username is used directly as the identity key,
    consistent with how every other module already treats it as the
    natural identifier.

    target_type distinguishes "artist" from "album" feedback, since they
    have different downstream effects: a disliked ARTIST is excluded
    entirely from future recommendations (folded into known_artists at
    walk time), while a disliked ALBUM is only down-weighted in entry-
    point selection, not treated as a full exclusion - disliking one
    album by an artist you otherwise like shouldn't blacklist that artist.
    Liked feedback (either type) gives a small positive boost rather than
    any structural change.

    For album feedback, artist_name is also stored (not just the album
    name) since album titles aren't globally unique and entry-point
    scoring needs both to look up the right candidate.

    album_name uses an empty string sentinel (not NULL) for artist-type
    feedback - SQLite's UniqueConstraint treats NULL as distinct from any
    other NULL, so two artist-feedback rows for the same artist (both
    with album_name=NULL) would NOT collide and could be inserted twice.
    An empty string is a real, comparable value, so the constraint
    actually enforces "one feedback row per (user, target_type, artist,
    album)" the way it's meant to.
    """

    __tablename__ = "user_feedback"
    __table_args__ = (
        UniqueConstraint("username", "target_type", "artist_name", "album_name", name="uq_user_feedback_target"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False)  # "artist" or "album"
    artist_name = Column(String, nullable=False)
    album_name = Column(String, nullable=False, default="")  # "" sentinel for artist-type feedback, see above
    sentiment = Column(String, nullable=False)  # "liked" or "disliked"
    created_at = Column(DateTime, nullable=False, default=utcnow)


def get_engine(database_path: str):
    # default pool (size=5, overflow=10, 15 total) is far too small for the
    # discovery walk's concurrent hop expansion, which can issue dozens of
    # simultaneous get_similar_artists calls (each opening its own session)
    # within a single asyncio.gather batch. Raised here AND the cache layer
    # adds its own semaphore (see ArtistDataCache) as a second line of
    # defense, since SQLite itself doesn't handle large numbers of truly
    # concurrent connections gracefully regardless of pool size.
    return create_engine(f"sqlite:///{database_path}", pool_size=20, max_overflow=20, pool_timeout=30)


def init_db(database_path: str):
    engine = get_engine(database_path)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(database_path: str):
    engine = init_db(database_path)
    return sessionmaker(bind=engine)
