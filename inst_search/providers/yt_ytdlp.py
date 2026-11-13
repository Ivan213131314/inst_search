"""YouTube через yt-dlp: без API-ключа и без дневной квоты.

Три режима:
  search()        - плоский поиск по ключевой фразе (дёшево, даёт обрезанное описание)
  enrich_video()  - полные метаданные одного видео (описание целиком, лайки, подписчики канала)
  channel_feed()  - лента канала (обычные видео или shorts)
"""
import time
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

_FLAT = {
    "quiet": True, "no_warnings": True, "skip_download": True,
    "extract_flat": "in_playlist", "ignoreerrors": True,
    "socket_timeout": 30, "retries": 2,
}
_FULL = {
    "quiet": True, "no_warnings": True, "skip_download": True,
    "ignoreerrors": True, "socket_timeout": 30, "retries": 2,
}


def _entries(target, opts):
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(target, download=False)
        except DownloadError:
            return {}, []
    if not info:
        return {}, []
    return info, [e for e in (info.get("entries") or []) if e]


def search(query, limit=30, shorts_only=False):
    """Плоский поиск. shorts_only фильтрует по длительности <= 90 c."""
    info, entries = _entries(f"ytsearch{limit}:{query}", _FLAT)
    out = []
    for e in entries:
        dur = e.get("duration")
        if shorts_only and (dur is None or dur > 90):
            continue
        out.append(_norm_video(e, query=query))
    return out


def channel_feed(channel_url, limit=30, tab="videos"):
    """tab: videos | shorts | streams. Возвращает (meta_канала, [видео])."""
    url = channel_url.rstrip("/")
    if not url.endswith(("/videos", "/shorts", "/streams")):
        url = f"{url}/{tab}"
    opts = dict(_FLAT, playlistend=limit)
    info, entries = _entries(url, opts)
    meta = {
        "id": info.get("channel_id") or info.get("uploader_id"),
        "name": info.get("channel") or info.get("uploader"),
        "url": info.get("channel_url") or info.get("uploader_url") or channel_url,
        "followers": info.get("channel_follower_count"),
        "bio": info.get("description"),
        "tags": info.get("tags"),
    }
    return meta, [_norm_video(e) for e in entries]


def enrich_video(video_id):
    """Полное описание + лайки + число подписчиков канала. ~1 запрос на видео."""
    info, _ = _entries(f"https://www.youtube.com/watch?v={video_id}", _FULL)
    if not info:
        return None
    v = _norm_video(info)
    v["followers"] = info.get("channel_follower_count")
    v["categories"] = info.get("categories")
    v["tags"] = info.get("tags")
    return v


def _norm_video(e, query=None):
    ts = e.get("timestamp") or e.get("release_timestamp")
    return {
        "id": e.get("id"),
        "platform": "youtube",
        "url": e.get("url") or e.get("webpage_url") or (f"https://www.youtube.com/watch?v={e.get('id')}"),
        "title": e.get("title"),
        "description": e.get("description") or "",
        "channel_id": e.get("channel_id") or e.get("uploader_id"),
        "channel_name": e.get("channel") or e.get("uploader"),
        "channel_url": e.get("channel_url") or e.get("uploader_url"),
        "view_count": e.get("view_count"),
        "like_count": e.get("like_count"),
        "comment_count": e.get("comment_count"),
        "duration": e.get("duration"),
        "published_at": time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else (e.get("upload_date") or None),
        "query": query,
        "raw": None,
    }
