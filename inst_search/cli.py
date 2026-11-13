"""CLI разведки лидген-контента в YouTube и Instagram."""
import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from . import store, scoring
from .providers import yt_ytdlp, yt_api, ig_apify

ROOT = Path(__file__).resolve().parent.parent


def _log(msg):
    print(msg, flush=True)


def _load_queries(path):
    with open(path or ROOT / "queries.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pick(cfg, platform, groups):
    """Разворачивает группы запросов в плоский список."""
    section = cfg.get(platform, {})
    if not groups:
        groups = list(section.keys())
    out = []
    for g in groups:
        out.extend(section.get(g, []))
    return out


# --- YouTube ---------------------------------------------------------------

def cmd_yt_discover(args):
    conn = store.connect(args.db)
    queries = args.query or _pick(_load_queries(args.queries_file), "youtube", args.group)
    _log(f"Запросов: {len(queries)}, лимит на запрос: {args.limit}")
    total = 0
    for i, q in enumerate(queries, 1):
        try:
            vids = yt_ytdlp.search(q, limit=args.limit, shorts_only=args.shorts)
        except Exception as e:
            _log(f"  [{i}/{len(queries)}] {q!r} -> ошибка: {e}")
            continue
        for v in vids:
            v["lang"] = scoring.detect_lang(f"{v.get('title')} {v.get('description')}")
            store.upsert_video(conn, v)
            if v.get("channel_id"):
                store.upsert_channel(conn, {
                    "id": v["channel_id"], "platform": "youtube",
                    "name": v.get("channel_name"), "url": v.get("channel_url"),
                    "hits": 1, "lang": v["lang"],
                })
        conn.commit()
        total += len(vids)
        _log(f"  [{i}/{len(queries)}] {q[:55]!r} -> {len(vids)}")
    _log(f"Готово. Видео записано/обновлено: {total}")


def cmd_yt_channels(args):
    """Подтягивает ленты найденных каналов, считает медиану просмотров и лидген-скор."""
    conn = store.connect(args.db)
    rows = conn.execute(
        """SELECT c.id, c.name, c.url, c.hits FROM channels c
           WHERE c.platform='youtube' AND (c.followers IS NULL OR ?)
           ORDER BY c.hits DESC LIMIT ?""",
        (1 if args.refresh else 0, args.top)).fetchall()
    _log(f"Каналов к обработке: {len(rows)}")
    for i, r in enumerate(rows, 1):
        url = r["url"]
        if not url:
            continue
        try:
            meta, vids = yt_ytdlp.channel_feed(url, limit=args.feed)
        except Exception as e:
            _log(f"  [{i}/{len(rows)}] {r['name']} -> ошибка: {e}")
            continue
        for v in vids:
            v["lang"] = scoring.detect_lang(f"{v.get('title')} {v.get('description')}")
            v["channel_id"] = v.get("channel_id") or r["id"]
            v["channel_name"] = v.get("channel_name") or r["name"]
            v["channel_url"] = v.get("channel_url") or url
            store.upsert_video(conn, v)
        med, tot, _ = scoring.traction([v.get("view_count") for v in vids], meta.get("followers"))
        # Лента канала описаний не отдаёт, зато они уже есть в базе с этапа discovery.
        titles, descs = _channel_texts(conn, r["id"], vids)
        score, signals = scoring.leadgen_score(meta.get("bio"), titles, descs)
        store.upsert_channel(conn, {
            "id": r["id"], "platform": "youtube", "name": meta.get("name") or r["name"],
            "url": meta.get("url") or url, "followers": meta.get("followers"),
            "bio": meta.get("bio"), "hits": 0, "median_views": med, "total_views": tot,
            "leadgen_score": score, "leadgen_signals": json.dumps(signals, ensure_ascii=False),
            "lang": scoring.detect_lang(f"{meta.get('bio')} {titles}"),
        })
        conn.commit()
        name = (meta.get("name") or r["name"] or "?")[:38]
        _log(f"  [{i}/{len(rows)}] {name:38} subs={meta.get('followers') or '?':>9} "
             f"med={med or '?':>8} score={score:5} {signals[:3]}")
        time.sleep(args.sleep)
    _log("Готово.")


def cmd_yt_enrich(args):
    """Полные описания через YouTube Data API v3 (1 юнит на 50 видео)."""
    if not yt_api.available():
        _log("YOUTUBE_API_KEY не задан - обогащение пропущено.")
        return
    conn = store.connect(args.db)
    rows = conn.execute(
        """SELECT id FROM videos WHERE platform='youtube'
           AND (description IS NULL OR length(description) < 200)
           ORDER BY view_count DESC LIMIT ?""", (args.top,)).fetchall()
    ids = [r["id"] for r in rows]
    _log(f"К обогащению: {len(ids)} видео (~{(len(ids) + 49) // 50} юнитов квоты)")
    data = yt_api.videos(ids)
    for vid, d in data.items():
        conn.execute(
            """UPDATE videos SET description=?, like_count=?, comment_count=?,
               view_count=COALESCE(?, view_count), published_at=COALESCE(?, published_at)
               WHERE id=?""",
            (d["description"], d["like_count"], d["comment_count"],
             d["view_count"], d["published_at"], vid))
    conn.commit()
    _log(f"Обогащено: {len(data)}")


def cmd_yt_shorts(args):
    """Собирает вкладку /shorts у отобранных каналов.

    Поисковая выдача YouTube для коротких роликов не отражает их реальные охваты:
    шортсы расходятся лентой Shorts, а не поиском. Честные цифры даёт только
    вкладка конкретного канала.
    """
    conn = store.connect(args.db)
    rows = conn.execute(
        """SELECT id, name, url FROM channels
           WHERE platform='youtube' AND url IS NOT NULL AND leadgen_score >= ?
           ORDER BY leadgen_score DESC LIMIT ?""", (args.min_score, args.top)).fetchall()
    _log(f"Каналов: {len(rows)} (лидген-скор >= {args.min_score})")
    total = 0
    for i, r in enumerate(rows, 1):
        try:
            _, vids = yt_ytdlp.channel_feed(r["url"], limit=args.feed, tab="shorts")
        except Exception as e:
            _log(f"  [{i}/{len(rows)}] {r['name']} -> ошибка: {e}")
            continue
        for v in vids:
            v["lang"] = scoring.detect_lang(v.get("title"))
            v["channel_id"] = v.get("channel_id") or r["id"]
            v["channel_name"] = v.get("channel_name") or r["name"]
            v["duration"] = v.get("duration") or 60  # вкладка shorts длительность не отдаёт
            store.upsert_video(conn, v)
        conn.commit()
        total += len(vids)
        _log(f"  [{i}/{len(rows)}] {(r['name'] or '?')[:34]:34} shorts={len(vids)}")
        time.sleep(args.sleep)
    _log(f"Готово. Шортсов собрано: {total}")


def _channel_texts(conn, channel_id, extra_videos=()):
    """Все заголовки и описания видео канала из базы плюс то, что пришло сейчас."""
    rows = conn.execute(
        "SELECT title, description FROM videos WHERE channel_id=?", (channel_id,)).fetchall()
    titles = [r["title"] or "" for r in rows] + [v.get("title") or "" for v in extra_videos]
    descs = [r["description"] or "" for r in rows] + [v.get("description") or "" for v in extra_videos]
    return " ".join(titles), " ".join(descs)


def cmd_rescore(args):
    """Пересчёт лидген-скора по уже собранным данным. Сеть не трогает."""
    conn = store.connect(args.db)
    rows = conn.execute(
        "SELECT id, bio, external_url, platform FROM channels WHERE followers IS NOT NULL").fetchall()
    changed = 0
    for r in rows:
        titles, descs = _channel_texts(conn, r["id"])
        score, signals = scoring.leadgen_score(r["bio"], r["external_url"], titles, descs)
        conn.execute("UPDATE channels SET leadgen_score=?, leadgen_signals=? WHERE id=?",
                     (score, json.dumps(signals, ensure_ascii=False), r["id"]))
        changed += 1
    conn.commit()
    _log(f"Пересчитано каналов: {changed}")
    high = conn.execute("SELECT COUNT(*) n FROM channels WHERE leadgen_score>=50").fetchone()["n"]
    _log(f"С лидген-скором >= 50: {high}")


# --- Instagram -------------------------------------------------------------

IG_PRICE_PER_1K = 2.7  # free-план Apify, $ за 1000 результатов


def cmd_ig_check(args):
    """Проверка токена Apify и остатка бесплатных кредитов. Ничего не тратит."""
    if not ig_apify.available():
        _log("APIFY_TOKEN не задан.\n"
             "1) Зарегистрируйся на apify.com\n"
             "2) Settings -> API & Integrations -> Personal API tokens\n"
             "3) setx APIFY_TOKEN <token>  и перезапусти терминал")
        return
    try:
        info = ig_apify.account_info()
    except Exception as e:
        _log(f"Токен задан, но проверка не прошла: {e}")
        return
    _log(f"Аккаунт: {info['username']} | план: {info['plan']}")
    used, cap = info.get("monthly_usage_usd"), info.get("max_monthly_usage_usd")
    if used is not None:
        left = (cap - used) if cap else None
        _log(f"Израсходовано за месяц: ${used:.2f}" + (f" из ${cap:.2f}" if cap else ""))
        if left is not None:
            _log(f"Осталось: ${left:.2f} = примерно {int(left / IG_PRICE_PER_1K * 1000)} результатов")


def cmd_ig_discover(args):
    if not ig_apify.available():
        _log("APIFY_TOKEN не задан. Запусти сначала: ig-check")
        return
    conn = store.connect(args.db)
    cfg = _load_queries(args.queries_file)
    groups = [g for g in (args.group or []) if "hashtag" in g] or ["hashtags_en", "hashtags_ru"]
    tags = args.hashtag or _pick(cfg, "instagram", groups)
    planned = len(tags) * args.limit
    _log(f"Хэштегов: {len(tags)}, лимит на хэштег: {args.limit}")
    _log(f"Ожидаемый расход: до {planned} результатов = примерно ${planned / 1000 * IG_PRICE_PER_1K:.2f}")
    if planned > args.budget:
        _log(f"Отменено: план ({planned}) превышает --budget ({args.budget}). "
             f"Уменьши --limit или подними --budget осознанно.")
        return
    for i, t in enumerate(tags, 1):
        try:
            posts = ig_apify.search_hashtag(t, limit=args.limit)
        except Exception as e:
            _log(f"  [{i}/{len(tags)}] #{t} -> ошибка: {e}")
            continue
        for p in posts:
            p["lang"] = scoring.detect_lang(p.get("description"))
            p["raw"] = None
            store.upsert_video(conn, p)
            if p.get("channel_id"):
                store.upsert_channel(conn, {
                    "id": p["channel_id"], "platform": "instagram",
                    "handle": p.get("channel_name"), "name": p.get("channel_name"),
                    "url": p.get("channel_url"), "hits": 1, "lang": p["lang"],
                })
        conn.commit()
        _log(f"  [{i}/{len(tags)}] #{t} -> {len(posts)}")
    _log("Готово.")


def cmd_ig_seed(args):
    """Добавляет аккаунты в очередь на профилирование, ничего не тратя.

    Хэштеги дают много международного шума, поэтому список кандидатов удобнее
    задавать руками: из ig-discover, из шапок YouTube-каналов или просто по именам.
    """
    conn = store.connect(args.db)
    handles = [h.lstrip("@").strip("/ ").split("/")[-1] for h in args.handles if h.strip()]
    for h in handles:
        store.upsert_channel(conn, {
            "id": h, "platform": "instagram", "handle": h, "name": h,
            "url": f"https://www.instagram.com/{h}/", "hits": 1,
        })
        # Метка ставится отдельно: upsert не перезаписывает её у существующих записей,
        # а посев должен иметь приоритет в очереди даже при повторном добавлении.
        conn.execute("UPDATE channels SET source='seed' WHERE id=? AND platform='instagram'", (h,))
    conn.commit()
    _log(f"Добавлено в очередь: {len(handles)}")
    _log("Дальше: ig-profiles --top N")


def cmd_ig_profiles(args):
    if not ig_apify.available():
        _log("APIFY_TOKEN не задан. Запусти сначала: ig-check")
        return
    conn = store.connect(args.db)
    # Профилирование стоит денег, поэтому очередь строим по предварительному скору
    # подписей, собранных бесплатно на этапе ig-discover, а не по числу попаданий.
    candidates = conn.execute(
        """SELECT id, handle, source FROM channels WHERE platform='instagram'
           AND handle IS NOT NULL AND (followers IS NULL OR ?)""",
        (1 if args.refresh else 0,)).fetchall()
    ranked = []
    for r in candidates:
        if r["source"] == "seed":
            # Аккаунт выбран вручную через ig-seed, поэтому идёт впереди
            # хэштеговой массы независимо от того, есть ли у него посты в базе.
            ranked.append((10_000, r))
            continue
        caps = conn.execute(
            "SELECT GROUP_CONCAT(description, ' ') c FROM videos WHERE channel_id=?",
            (r["id"],)).fetchone()["c"]
        pre, _ = scoring.leadgen_score(caps)
        ranked.append((pre, r))
    ranked.sort(key=lambda x: -x[0])
    rows = [r for pre, r in ranked[:args.top]]
    planned = len(rows) * (args.feed + 1)  # худший случай: все прошли порог
    _log(f"Профилей к обработке: {len(rows)}, порог по подписчикам: {args.min_followers}")
    fails = 0
    _log(f"Расход: минимум {len(rows)} результатов (${len(rows) / 1000 * IG_PRICE_PER_1K:.2f}), "
         f"в худшем случае {planned} (${planned / 1000 * IG_PRICE_PER_1K:.2f})")
    if planned > args.budget:
        _log(f"Отменено: план ({planned}) превышает --budget ({args.budget}).")
        return
    for i, r in enumerate(rows, 1):
        handle = r["handle"]
        if not handle:
            continue
        # Карточка профиля стоит 1 результат, лента - args.feed. Поэтому сначала
        # смотрим размер аккаунта и только потом решаем, платить ли за ленту:
        # хэштеги приводят массу студий-новичков с парой десятков подписчиков,
        # и как референс они бесполезны.
        try:
            prof = ig_apify.profile(handle)
            fails = 0
        except Exception as e:
            fails += 1
            _log(f"  [{i}/{len(rows)}] @{handle} -> ошибка: {str(e)[:120]}")
            if fails >= args.max_fails:
                _log(f"Прервано: {fails} ошибок подряд, похоже на обрыв сети. "
                     f"Проверь связь и запусти команду снова, уже обработанные пропустятся.")
                return
            continue
        if not prof:
            continue
        followers = prof.get("followers") or 0
        if followers < args.min_followers:
            store.upsert_channel(conn, {
                "id": r["id"], "platform": "instagram", "handle": handle,
                "name": prof.get("name"), "url": prof.get("url"), "followers": followers,
                "bio": prof.get("bio"), "external_url": prof.get("external_url"),
                "hits": 0, "leadgen_score": 0.0, "leadgen_signals": "[]",
            })
            conn.commit()
            _log(f"  [{i}/{len(rows)}] @{handle:22} followers={followers:>9} - пропуск, ниже порога")
            continue
        try:
            posts = ig_apify.profile_posts(handle, limit=args.feed)
        except Exception as e:
            _log(f"  [{i}/{len(rows)}] @{handle} -> лента не получена: {e}")
            posts = []
        for p in posts:
            p["lang"] = scoring.detect_lang(p.get("description"))
            p["raw"] = None
            # Пост приходит с числовым ownerId, а канал заведён под хендлом
            # (через ig-seed). Без выравнивания связь постов с каналом рвётся.
            p["channel_id"] = r["id"]
            p["channel_name"] = p.get("channel_name") or handle
            store.upsert_video(conn, p)
        med, tot, _ = scoring.traction([p.get("view_count") for p in posts], prof.get("followers"))
        caps = " ".join((p.get("description") or "") for p in posts)
        score, signals = scoring.leadgen_score(prof.get("bio"), prof.get("external_url"), caps)
        store.upsert_channel(conn, {
            "id": r["id"], "platform": "instagram", "handle": handle,
            "name": prof.get("name"), "url": prof.get("url"),
            "followers": prof.get("followers"), "bio": prof.get("bio"),
            "external_url": prof.get("external_url"), "hits": 0,
            "median_views": med, "total_views": tot, "leadgen_score": score,
            "leadgen_signals": json.dumps(signals, ensure_ascii=False),
            "lang": scoring.detect_lang(f"{prof.get('bio')} {caps}"),
        })
        conn.commit()
        _log(f"  [{i}/{len(rows)}] @{handle:22} followers={prof.get('followers') or '?':>9} "
             f"med={med or '?':>8} score={score:5} {signals[:3]}")
    _log("Готово.")


# --- Аналитика -------------------------------------------------------------

def cmd_formats(args):
    """Какие форматы видео реально набирают просмотры в собранной выборке."""
    conn = store.connect(args.db)
    where, params = "WHERE 1=1", []
    if args.platform:
        where += " AND platform=?"
        params.append(args.platform)
    if args.lang:
        where += " AND lang=?"
        params.append(args.lang)
    rows = conn.execute(
        f"SELECT title, description, duration, view_count FROM videos {where}", params).fetchall()
    buckets = defaultdict(list)
    for r in rows:
        if not r["view_count"]:
            continue
        for tag in scoring.classify_format(r["title"], r["description"], r["duration"]):
            buckets[tag].append(r["view_count"])
    _log(f"\nВыборка: {len(rows)} видео"
         + (f" | платформа: {args.platform}" if args.platform else "")
         + (f" | язык: {args.lang}" if args.lang else ""))
    _log(f"{'формат':22} {'n':>6} {'медиана':>12} {'p90':>12}")
    _log("-" * 56)
    for tag, vals in sorted(buckets.items(), key=lambda kv: -statistics.median(kv[1])):
        if len(vals) < args.min_n:
            continue
        vals.sort()
        p90 = vals[int(len(vals) * 0.9) - 1] if len(vals) >= 10 else vals[-1]
        _log(f"{tag:22} {len(vals):>6} {int(statistics.median(vals)):>12,} {p90:>12,}")


def cmd_cohorts(args):
    """Сравнивает группы аккаунтов: охват относительно аудитории и наличие воронки.

    Отвечает на вопрос "работает ли ниша X для продажи услуг", а не просто
    "сколько там просмотров": ниша может собирать охват и при этом не продавать.
    """
    conn = store.connect(args.db)
    cohorts = _load_queries(args.queries_file).get("ig_cohorts", {})
    if not cohorts:
        _log("В queries.yaml нет секции ig_cohorts.")
        return
    _log(f"{'когорта':20} {'акк':>4} {'медиана подп':>13} {'охват/ауд':>10} "
         f"{'лидген-скор':>12} {'с воронкой':>11}")
    _log("-" * 76)
    for name, handles in cohorts.items():
        rows = []
        for h in handles:
            r = conn.execute(
                """SELECT followers, median_views, leadgen_score FROM channels
                   WHERE platform='instagram' AND handle=? AND followers IS NOT NULL""",
                (h,)).fetchone()
            if r:
                rows.append(r)
        if not rows:
            _log(f"{name:20} нет данных")
            continue
        subs = [r["followers"] for r in rows if r["followers"]]
        ratios = [r["median_views"] / r["followers"] for r in rows
                  if r["median_views"] and r["followers"]]
        scores = [r["leadgen_score"] for r in rows if r["leadgen_score"] is not None]
        funnel = sum(1 for s in scores if s >= 40)
        _log(f"{name:20} {len(rows):>4} {int(statistics.median(subs)) if subs else 0:>13,} "
             f"{statistics.median(ratios) if ratios else 0:>10.3f} "
             f"{statistics.median(scores) if scores else 0:>12.1f} "
             f"{funnel:>6}/{len(rows):<4}")


def cmd_report(args):
    conn = store.connect(args.db)
    where, params = "WHERE leadgen_score IS NOT NULL", []
    if args.platform:
        where += " AND platform=?"
        params.append(args.platform)
    if args.lang:
        where += " AND lang=?"
        params.append(args.lang)
    if args.min_followers:
        where += " AND followers >= ?"
        params.append(args.min_followers)
    if args.max_followers:
        where += " AND followers <= ?"
        params.append(args.max_followers)
    rows = conn.execute(
        f"""SELECT * FROM channels {where}
            ORDER BY leadgen_score DESC, median_views DESC LIMIT ?""",
        params + [args.top]).fetchall()

    lines = ["# Референсы: кто продаёт разработку через контент", "",
             f"Собрано: {time.strftime('%Y-%m-%d %H:%M')} | каналов в отчёте: {len(rows)}", ""]
    for r in rows:
        name = r["name"] or r["handle"] or r["id"]
        lines.append(f"## {name}  ({r['platform']})")
        lines.append(f"- Ссылка: {r['url']}")
        lines.append(f"- Аудитория: {r['followers'] or '?'} | медиана просмотров: {r['median_views'] or '?'}")
        ratio = (r["median_views"] / r["followers"]) if (r["median_views"] and r["followers"]) else None
        if ratio:
            note = "  (контент выходит за пределы подписчиков)" if ratio > 0.5 else ""
            lines.append(f"- Охват/аудитория: {ratio:.2f}{note}")
        lines.append(f"- Лидген-скор: {r['leadgen_score']} | сигналы: {r['leadgen_signals']}")
        if r["external_url"]:
            lines.append(f"- Внешняя ссылка: {r['external_url']}")
        bio = (r["bio"] or "").strip().replace("\n", " ")
        if bio:
            lines.append(f"- Био: {bio[:400]}")
        top = conn.execute(
            """SELECT title, view_count, url, duration FROM videos
               WHERE channel_id=? AND view_count IS NOT NULL
               ORDER BY view_count DESC LIMIT 3""", (r["id"],)).fetchall()
        if top:
            lines.append("- Топ-видео:")
            for t in top:
                fmt = ",".join(scoring.classify_format(t["title"], None, t["duration"]))
                lines.append(f"  - {t['view_count']:,} | {t['title']} | [{fmt}] | {t['url']}")
        lines.append("")
    text = "\n".join(lines)
    out = Path(args.out or ROOT / "data" / "report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _log(f"Отчёт записан: {out}  ({len(rows)} каналов)")
    if args.print:
        _log("\n" + text[:6000])


def cmd_stats(args):
    conn = store.connect(args.db)
    for platform in ("youtube", "instagram"):
        v = conn.execute("SELECT COUNT(*) n FROM videos WHERE platform=?", (platform,)).fetchone()["n"]
        c = conn.execute("SELECT COUNT(*) n FROM channels WHERE platform=?", (platform,)).fetchone()["n"]
        s = conn.execute("SELECT COUNT(*) n FROM channels WHERE platform=? AND leadgen_score>=50",
                         (platform,)).fetchone()["n"]
        _log(f"{platform:10} видео={v:6} каналов={c:5} из них лидген-скор>=50: {s}")


def build_parser():
    p = argparse.ArgumentParser(
        prog="inst_search",
        description="Разведка контента, который приводит клиентов на разработку")
    p.add_argument("--db", default=None, help="путь к SQLite (по умолчанию data/leadgen.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("yt-discover", help="поиск видео по запросам (yt-dlp, без квоты)")
    d.add_argument("--queries-file")
    d.add_argument("--group", nargs="*")
    d.add_argument("--query", nargs="*", help="разовые запросы вместо файла")
    d.add_argument("--limit", type=int, default=25)
    d.add_argument("--shorts", action="store_true", help="только ролики <= 90 c")
    d.set_defaults(func=cmd_yt_discover)

    c = sub.add_parser("yt-channels", help="подтянуть ленты каналов и посчитать лидген-скор")
    c.add_argument("--top", type=int, default=40)
    c.add_argument("--feed", type=int, default=20)
    c.add_argument("--sleep", type=float, default=1.0)
    c.add_argument("--refresh", action="store_true", help="обновить и уже обработанные")
    c.set_defaults(func=cmd_yt_channels)

    sh = sub.add_parser("yt-shorts", help="собрать вкладку /shorts у отобранных каналов")
    sh.add_argument("--top", type=int, default=25)
    sh.add_argument("--feed", type=int, default=30)
    sh.add_argument("--min-score", type=float, default=35.0)
    sh.add_argument("--sleep", type=float, default=0.5)
    sh.set_defaults(func=cmd_yt_shorts)

    e = sub.add_parser("yt-enrich", help="полные описания через YouTube Data API v3")
    e.add_argument("--top", type=int, default=500)
    e.set_defaults(func=cmd_yt_enrich)

    ic = sub.add_parser("ig-check", help="проверить токен Apify и остаток кредитов")
    ic.set_defaults(func=cmd_ig_check)

    isd = sub.add_parser("ig-seed", help="добавить аккаунты в очередь профилирования (бесплатно)")
    isd.add_argument("handles", nargs="+", help="хендлы или ссылки на профили")
    isd.set_defaults(func=cmd_ig_seed)

    i = sub.add_parser("ig-discover", help="reels по хэштегам (Apify)")
    i.add_argument("--queries-file")
    i.add_argument("--group", nargs="*")
    i.add_argument("--hashtag", nargs="*")
    i.add_argument("--limit", type=int, default=40)
    i.add_argument("--budget", type=int, default=600,
                   help="максимум результатов за прогон, защита от перерасхода кредитов")
    i.set_defaults(func=cmd_ig_discover)

    ip = sub.add_parser("ig-profiles", help="профили + ленты reels, лидген-скор")
    ip.add_argument("--top", type=int, default=30)
    ip.add_argument("--feed", type=int, default=18)
    ip.add_argument("--refresh", action="store_true")
    ip.add_argument("--max-fails", type=int, default=4,
                    help="прервать прогон после стольких сетевых ошибок подряд")
    ip.add_argument("--min-followers", type=int, default=3000,
                    help="ленту не запрашиваем у аккаунтов меньше этого размера")
    ip.add_argument("--budget", type=int, default=600,
                    help="максимум результатов за прогон, защита от перерасхода кредитов")
    ip.set_defaults(func=cmd_ig_profiles)

    f = sub.add_parser("formats", help="какие форматы набирают просмотры")
    f.add_argument("--platform")
    f.add_argument("--lang")
    f.add_argument("--min-n", type=int, default=5)
    f.set_defaults(func=cmd_formats)

    r = sub.add_parser("report", help="markdown-отчёт по референсам")
    r.add_argument("--platform")
    r.add_argument("--lang")
    r.add_argument("--top", type=int, default=40)
    r.add_argument("--min-followers", type=int)
    r.add_argument("--max-followers", type=int)
    r.add_argument("--out")
    r.add_argument("--print", action="store_true")
    r.set_defaults(func=cmd_report)

    co = sub.add_parser("cohorts", help="сравнить группы аккаунтов из ig_cohorts в queries.yaml")
    co.add_argument("--queries-file")
    co.set_defaults(func=cmd_cohorts)

    rs = sub.add_parser("rescore", help="пересчитать лидген-скор по собранным данным (без сети)")
    rs.set_defaults(func=cmd_rescore)

    s = sub.add_parser("stats", help="что уже собрано")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv=None):
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
