"""Instagram через Apify.

Почему не бесплатно: публичный веб-эндпоинт Instagram (users/web_profile_info)
с чистого IP отдаёт 429, instaloader/instagrapi требуют аккаунт и рискуют баном.
Apify держит пул прокси и не требует логина. Free-план даёт $5 кредитов в месяц,
актор apify/instagram-scraper стоит ~$2.7/1000 результатов на free-плане,
то есть примерно 1800 постов в месяц бесплатно - для разведки этого хватает.

Токен: переменная окружения APIFY_TOKEN (Apify Console -> Settings -> API tokens).
"""
import os
import requests

RUN_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

ACTOR_MAIN = "apify~instagram-scraper"
ACTOR_HASHTAG = "apify~instagram-hashtag-scraper"


def available():
    return bool(os.environ.get("APIFY_TOKEN"))


def account_info():
    """Проверка токена и остатка кредитов. Запрос бесплатный."""
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return None
    r = requests.get("https://api.apify.com/v2/users/me", params={"token": token}, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", {})
    usage = {}
    try:
        u = requests.get("https://api.apify.com/v2/users/me/limits",
                         params={"token": token}, timeout=30)
        if u.ok:
            usage = u.json().get("data", {})
    except requests.RequestException:
        pass
    return {
        "username": data.get("username"),
        "plan": (data.get("plan") or {}).get("id"),
        "monthly_usage_usd": (usage.get("current") or {}).get("monthlyUsageUsd"),
        "max_monthly_usage_usd": (usage.get("limits") or {}).get("maxMonthlyUsageUsd"),
    }


def _run(actor, payload, timeout=600):
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN не задан")
    r = requests.post(RUN_SYNC.format(actor=actor), params={"token": token},
                      json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def search_hashtag(hashtag, limit=50, newer_than=None):
    """Reels по хэштегу.

    Через `search`+`searchType=hashtag` актор возвращает только карточки найденных
    хэштегов и вдобавок промахивается по названию (запрос vibecoding отдал vibetheoryai).
    Прямой URL тега заставляет его скрапить сами посты.
    """
    tag = hashtag.lstrip("#")
    payload = {
        "directUrls": [f"https://www.instagram.com/explore/tags/{tag}/"],
        "resultsType": "posts",
        "resultsLimit": limit,
        "skipPinnedPosts": True,
        "addParentData": False,
    }
    if newer_than:
        payload["onlyPostsNewerThan"] = newer_than
    items = _run(ACTOR_MAIN, payload)
    return [_norm_post(i, query=f"#{tag}") for i in items if isinstance(i, dict)]


def search_users(keyword, limit=20):
    """Поиск аккаунтов по ключевому слову (профили без постов)."""
    items = _run(ACTOR_MAIN, {
        "search": keyword,
        "searchType": "user",
        "searchLimit": limit,
        "resultsType": "details",
        "resultsLimit": 1,
    })
    return [_norm_profile(i) for i in items if isinstance(i, dict)]


def profile_posts(username, limit=30, only_reels=True):
    """Лента конкретного аккаунта. Именно отсюда берём медианные просмотры reels."""
    items = _run(ACTOR_MAIN, {
        "directUrls": [f"https://www.instagram.com/{username.lstrip('@')}/"],
        "resultsType": "reels" if only_reels else "posts",
        "resultsLimit": limit,
        "skipPinnedPosts": True,
        "addParentData": True,
    })
    return [_norm_post(i, query=f"@{username}") for i in items if isinstance(i, dict)]


def profile(username):
    """Карточка профиля: подписчики, bio, внешняя ссылка."""
    items = _run(ACTOR_MAIN, {
        "directUrls": [f"https://www.instagram.com/{username.lstrip('@')}/"],
        "resultsType": "details",
        "resultsLimit": 1,
    })
    for i in items:
        if isinstance(i, dict):
            return _norm_profile(i)
    return None


def _norm_post(i, query=None):
    owner = i.get("ownerUsername") or (i.get("owner") or {}).get("username")
    return {
        "id": i.get("id") or i.get("shortCode"),
        "platform": "instagram",
        "url": i.get("url") or (f"https://www.instagram.com/p/{i.get('shortCode')}/" if i.get("shortCode") else None),
        "title": (i.get("caption") or "")[:200],
        "description": i.get("caption") or "",
        "channel_id": str(i.get("ownerId") or owner or ""),
        "channel_name": owner,
        "channel_url": f"https://www.instagram.com/{owner}/" if owner else None,
        "view_count": i.get("videoPlayCount") or i.get("videoViewCount"),
        "like_count": i.get("likesCount"),
        "comment_count": i.get("commentsCount"),
        "duration": int(i["videoDuration"]) if i.get("videoDuration") else None,
        "published_at": (i.get("timestamp") or "")[:10],
        "query": query,
        "raw": i,
    }


def _norm_profile(i):
    return {
        "id": str(i.get("id") or i.get("username")),
        "platform": "instagram",
        "handle": i.get("username"),
        "name": i.get("fullName"),
        "url": i.get("url") or (f"https://www.instagram.com/{i.get('username')}/" if i.get("username") else None),
        "followers": i.get("followersCount"),
        "bio": i.get("biography") or "",
        "external_url": i.get("externalUrl") or (i.get("externalUrls") or [{}])[0].get("url"),
        "raw": i,
    }
