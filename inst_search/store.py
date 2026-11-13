"""SQLite-хранилище результатов разведки."""
import sqlite3
import json
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "leadgen.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    description   TEXT,
    channel_id    TEXT,
    channel_name  TEXT,
    channel_url   TEXT,
    view_count    INTEGER,
    like_count    INTEGER,
    comment_count INTEGER,
    duration      INTEGER,
    published_at  TEXT,
    query         TEXT,
    lang          TEXT,
    raw           TEXT,
    fetched_at    INTEGER
);

CREATE TABLE IF NOT EXISTS channels (
    id             TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,
    handle         TEXT,
    name           TEXT,
    url            TEXT,
    followers      INTEGER,
    bio            TEXT,
    external_url   TEXT,
    hits           INTEGER DEFAULT 0,
    median_views   INTEGER,
    total_views    INTEGER,
    leadgen_score  REAL,
    leadgen_signals TEXT,
    lang           TEXT,
    raw            TEXT,
    fetched_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_views   ON videos(view_count DESC);
CREATE INDEX IF NOT EXISTS idx_ch_score       ON channels(leadgen_score DESC);
"""


def connect(db_path=None):
    path = Path(db_path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Добавляет колонки, появившиеся после создания базы."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(channels)")}
    if "source" not in have:
        conn.execute("ALTER TABLE channels ADD COLUMN source TEXT")
        conn.commit()


def upsert_video(conn, v):
    conn.execute(
        """INSERT INTO videos (id, platform, url, title, description, channel_id, channel_name,
                               channel_url, view_count, like_count, comment_count, duration,
                               published_at, query, lang, raw, fetched_at)
           VALUES (:id,:platform,:url,:title,:description,:channel_id,:channel_name,:channel_url,
                   :view_count,:like_count,:comment_count,:duration,:published_at,:query,:lang,:raw,:fetched_at)
           ON CONFLICT(id) DO UPDATE SET
                view_count    = COALESCE(excluded.view_count, videos.view_count),
                like_count    = COALESCE(excluded.like_count, videos.like_count),
                comment_count = COALESCE(excluded.comment_count, videos.comment_count),
                description   = COALESCE(NULLIF(excluded.description,''), videos.description),
                fetched_at    = excluded.fetched_at""",
        _defaults(v, "video"),
    )


def upsert_channel(conn, c):
    conn.execute(
        """INSERT INTO channels (id, platform, handle, name, url, followers, bio, external_url,
                                 hits, median_views, total_views, leadgen_score, leadgen_signals,
                                 lang, raw, fetched_at)
           VALUES (:id,:platform,:handle,:name,:url,:followers,:bio,:external_url,:hits,
                   :median_views,:total_views,:leadgen_score,:leadgen_signals,:lang,:raw,:fetched_at)
           ON CONFLICT(id) DO UPDATE SET
                followers       = COALESCE(excluded.followers, channels.followers),
                bio             = COALESCE(NULLIF(excluded.bio,''), channels.bio),
                external_url    = COALESCE(excluded.external_url, channels.external_url),
                hits            = channels.hits + excluded.hits,
                median_views    = COALESCE(excluded.median_views, channels.median_views),
                total_views     = COALESCE(excluded.total_views, channels.total_views),
                leadgen_score   = COALESCE(excluded.leadgen_score, channels.leadgen_score),
                leadgen_signals = COALESCE(excluded.leadgen_signals, channels.leadgen_signals),
                fetched_at      = excluded.fetched_at""",
        _defaults(c, "channel"),
    )


_VIDEO_KEYS = ["id", "platform", "url", "title", "description", "channel_id", "channel_name",
               "channel_url", "view_count", "like_count", "comment_count", "duration",
               "published_at", "query", "lang", "raw", "fetched_at"]
_CHANNEL_KEYS = ["id", "platform", "handle", "name", "url", "followers", "bio", "external_url",
                 "hits", "median_views", "total_views", "leadgen_score", "leadgen_signals",
                 "lang", "raw", "fetched_at"]


def _defaults(d, kind):
    keys = _VIDEO_KEYS if kind == "video" else _CHANNEL_KEYS
    out = {k: d.get(k) for k in keys}
    out["fetched_at"] = out.get("fetched_at") or int(time.time())
    if kind == "channel" and out.get("hits") is None:
        out["hits"] = 0
    if isinstance(out.get("raw"), (dict, list)):
        out["raw"] = json.dumps(out["raw"], ensure_ascii=False)
    return out
