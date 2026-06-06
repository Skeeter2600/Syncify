from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import SQLModel, Field, create_engine, Session, Relationship
from sqlalchemy import text
from config import Config



class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    jellyfin_user_id: str = Field(unique=True, index=True)
    jellyfin_username: str
    listenbrainz_token: Optional[str] = Field(default=None, nullable=True)
    listenbrainz_user: Optional[str] = Field(default=None, nullable=True)
    linked_at: Optional[datetime] = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    playlists: List["PlaylistConfig"] = Relationship(back_populates="user", cascade_delete=True)
    downloads: List["DownloadQueue"] = Relationship(back_populates="user", cascade_delete=True)
    listen_syncs: List["ListenSync"] = Relationship(back_populates="user", cascade_delete=True)
    playlist_jobs: List["PlaylistJob"] = Relationship(back_populates="user", cascade_delete=True)


class PlaylistConfig(SQLModel, table=True):
    __tablename__ = "playlist_config"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE")
    playlist_name: str
    theme: str
    song_count: int = Field(default=30)
    enabled: bool = Field(default=True)

    user: "User" = Relationship(back_populates="playlists")


class PlaylistJob(SQLModel, table=True):
    """Tracks a pending playlist that should be created once its downloads finish."""
    __tablename__ = "playlist_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    jellyfin_user_id: str
    playlist_name: str
    # JSON-serialised list of {"track_name", "artist_name", "album_name"} dicts
    lb_tracks_json: str
    # "waiting" -> downloads in progress; "created" -> done; "skipped" -> no tracks resolved
    status: str = Field(default="waiting", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None, nullable=True)

    user: "User" = Relationship(back_populates="playlist_jobs")
    download_items: List["DownloadQueue"] = Relationship(back_populates="playlist_job")


class DownloadQueue(SQLModel, table=True):
    __tablename__ = "download_queue"

    id: Optional[int] = Field(default=None, primary_key=True)
    track_name: str
    artist_name: str
    album_name: str
    deezer_url: Optional[str] = Field(default=None, nullable=True)
    # request_type: 'track' | 'album'
    request_type: str = Field(default="track", index=True)
    # status values: pending | downloading | done | failed
    status: str = Field(default="pending", index=True)
    # Optional context the user can supply when making a manual request
    notes: Optional[str] = Field(default=None, nullable=True)
    requested_by: int = Field(foreign_key="users.id", ondelete="CASCADE")
    # Links this item to a PlaylistJob so we know when all its downloads are done
    playlist_job_id: Optional[int] = Field(
        default=None, foreign_key="playlist_jobs.id", nullable=True, index=True
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None, nullable=True)

    user: "User" = Relationship(back_populates="downloads")
    playlist_job: Optional["PlaylistJob"] = Relationship(back_populates="download_items")


class ListenSync(SQLModel, table=True):
    __tablename__ = "listen_sync"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", index=True)
    last_synced_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tracks_submitted: int = Field(default=0)

    user: "User" = Relationship(back_populates="listen_syncs")


# SQLite needs check_same_thread=False for multi-threaded use
_connect_args = {"check_same_thread": False} if "sqlite" in Config.DATABASE_URL else {}
engine = create_engine(Config.DATABASE_URL, connect_args=_connect_args)


def _run_migrations() -> None:
    """
    Apply any schema changes that create_all won't handle (new columns on
    existing tables). Each statement is wrapped in its own try/except so a
    column that already exists is silently skipped.
    """
    migrations = [
        # Added in v2: link download items to a playlist job
        "ALTER TABLE download_queue ADD COLUMN playlist_job_id INTEGER REFERENCES playlist_jobs(id)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                # Column already exists or table doesn't exist yet — safe to ignore
                conn.rollback()


def init_db() -> None:
    Config.init_dirs()
    SQLModel.metadata.create_all(engine)
    _run_migrations()


def get_session():

    with Session(engine) as session:
        yield session
