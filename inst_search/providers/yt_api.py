"""YouTube Data API v3 - опциональное обогащение.

Смысл: поиск (search.list) стоит 100 юнитов и его мы делаем бесплатно через yt-dlp.
Дорогие данные (полное описание, лайки, комментарии) берём здесь: videos.list и
channels.list стоят 1 юнит за пачку до 50 объектов. Дневная квота 10 000 юнитов
= до 500 000 обогащённых видео в сутки.

Ключ: переменная окружения YOUTUBE_API_KEY.
"""
import os
import requests

BASE = "https://www.googleapis.com/youtube/v3"


def available():
    return bool(os.environ.get("YOUTUBE_API_KEY"))


def _get(endpoint, **params):
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY не задан")
    params["key"] = key
    r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def videos(video_ids):
    """1 юнит на каждые 50 id. Возвращает dict video_id -> данные."""
    out = {}
    ids = list(video_ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _get("videos", part="snippet,statistics,contentDetails", id=",".join(chunk))
        for item in data.get("items", []):
            sn, st = item.get("snippet", {}), item.get("statistics", {})
            out[item["id"]] = {
                "id": item["id"],
                "title": sn.get("title"),
                "description": sn.get("description"),
                "channel_id": sn.get("channelId"),
                "channel_name": sn.get("channelTitle"),
                "published_at": (sn.get("publishedAt") or "")[:10],
                "tags": sn.get("tags"),
                "view_count": _int(st.get("viewCount")),
                "like_count": _int(st.get("likeCount")),
                "comment_count": _int(st.get("commentCount")),
                "duration_iso": item.get("contentDetails", {}).get("duration"),
            }
    return out


def channels(channel_ids):
    """1 юнит на каждые 50 id. Даёт описание канала, счётчики и uploads-плейлист."""
    out = {}
    ids = list(channel_ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _get("channels", part="snippet,statistics,contentDetails,brandingSettings",
                    id=",".join(chunk))
        for item in data.get("items", []):
            sn, st = item.get("snippet", {}), item.get("statistics", {})
            out[item["id"]] = {
                "id": item["id"],
                "name": sn.get("title"),
                "handle": sn.get("customUrl"),
                "bio": sn.get("description"),
                "country": sn.get("country"),
                "published_at": (sn.get("publishedAt") or "")[:10],
                "followers": _int(st.get("subscriberCount")),
                "total_views": _int(st.get("viewCount")),
                "video_count": _int(st.get("videoCount")),
                "uploads_playlist": item.get("contentDetails", {})
                                        .get("relatedPlaylists", {}).get("uploads"),
                "keywords": item.get("brandingSettings", {}).get("channel", {}).get("keywords"),
            }
    return out


def uploads(playlist_id, limit=50):
    """Лента канала за 1 юнит на 50 видео (вместо 100 юнитов у search.list)."""
    out, token = [], None
    while len(out) < limit:
        data = _get("playlistItems", part="snippet,contentDetails",
                    playlistId=playlist_id, maxResults=min(50, limit - len(out)),
                    **({"pageToken": token} if token else {}))
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            out.append({
                "id": item.get("contentDetails", {}).get("videoId"),
                "title": sn.get("title"),
                "description": sn.get("description"),
                "published_at": (sn.get("publishedAt") or "")[:10],
            })
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
