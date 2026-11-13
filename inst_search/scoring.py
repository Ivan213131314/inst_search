"""Скоринг: продаёт ли автор услуги через контент (лидген) или это чистое медиа."""
import re
import statistics

# --- сигналы намерения продавать услуги -------------------------------------
# (regex, вес, метка)
SIGNALS = [
    # прямое бронирование звонка = самый сильный сигнал воронки
    (r"calendly\.com|cal\.com/|savvycal|tidycal|koalendar|book\.me", 30, "calendar-link"),
    (r"\b(book a call|book a free call|schedule a call|free consultation|free strategy call)\b", 28, "cta-book-call"),
    (r"(записаться на (звонок|консультацию)|бесплатн\w+ консультаци|обсудить проект|оставить заявку)", 28, "cta-book-call-ru"),
    # заявочные формы
    (r"typeform\.com|tally\.so|forms\.gle|airtable\.com/(app|shr)", 20, "lead-form"),
    # прямой найм
    (r"\b(work with me|hire me|hire us|let'?s work together|need a website|we build (apps|websites|software))\b", 22, "cta-hire"),
    (r"(нужен сайт|сделаю сайт|разработка под ключ|беру проекты|пишите в личку|заказать разработку)", 22, "cta-hire-ru"),
    # DM-воронка (типично для Instagram)
    (r"\b(dm me|send me a dm|comment ['\"]?\w+['\"]? (and i'?ll|to get)|reply \w+ (for|to get))\b", 18, "dm-funnel"),
    (r"(пиши[те]? в директ|напиши[те]? в личку|в директ слово)", 18, "dm-funnel-ru"),
    # собственное агентство / студия
    (r"\b(my agency|our agency|founder of|co-?founder of|ceo of|i (run|own|started|founded) (a|an|two|my)?\s*"
     r"(agency|studio|dev shop|service business|software company))\b", 15, "agency-owner"),
    (r"(основатель|сооснователь|моя студия|наше агентство|наша студия|моя команда разработ)", 15, "agency-owner-ru"),
    # публичные цифры выручки агентства = продажа своей модели
    (r"\$[\d,.]+\s*(k|m)?\s*(/\s*m|per month|a month|/mo|mrr|in revenue)", 12, "revenue-proof"),
    # лид-магнит в обмен на контакт
    (r"\b(free (template|guide|checklist|workflow|blueprint|resource|course|training)|"
     r"download the free|grab the free|link below to get)\b", 12, "lead-magnet"),
    (r"(бесплатн\w+ (шаблон|гайд|чек-?лист|разбор|курс|урок|вебинар|интенсив|мини-курс)|скачать шаблон|жми.{0,20}бесплатн)",
     12, "lead-magnet-ru"),
    # платформы монетизации сообщества (не услуги, но воронка та же)
    (r"skool\.com|whop\.com|gumroad\.com|patreon\.com|boosty\.to", 10, "community-monetization"),
    # В русскоязычном сегменте Telegram играет роль, которую в англоязычном играет Calendly:
    # ссылка в описании ведёт не на форму, а в личку или канал, где и происходит продажа.
    (r"t\.me/|telegram\.me/", 16, "telegram-funnel"),
    (r"(созвон|бриф|техзадание бесплатно|рассчитать стоимость|узнать цену|прайс в|"
     r"стоимость разработки|напиши мне|пиши мне|свяжитесь со мной)", 16, "cta-contact-ru"),
    # портфолио/кейсы
    (r"\b(case study|client project|built this for a client|portfolio)\b", 10, "case-study"),
    (r"(кейс|клиентский проект|портфолио)", 10, "case-study-ru"),
    (r"\b(newsletter|substack\.com|beehiiv\.com|kit\.com|convertkit)\b", 8, "newsletter"),
]

# то, что говорит "это медиа/образование, а не воронка услуг"
ANTI_SIGNALS = [
    (r"\b(sponsored by|this video is sponsored|use code \w+|affiliate link|brilliant\.org)\b", -6, "sponsorship-driven"),
    (r"\b(enroll now|my course|bootcamp|buy the course)\b", -4, "course-first"),
]

MAX_RAW = 65.0  # выше этого считаем 100

_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

# Авторы маскируют текст, подменяя кириллицу похожей латиницей ("БECПЛAТHЫЙ КУРС"),
# чтобы обойти фильтры площадок. Приводим такие символы обратно, иначе regex промахивается.
_HOMOGLYPHS = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
})

# Ссылка на собственный сайт (не соцсеть) - почти всегда точка входа воронки.
_URL = re.compile(r"https?://([\w.-]+)", re.IGNORECASE)
_NOT_OWN_DOMAIN = re.compile(
    r"(youtube|youtu\.be|instagram|tiktok|twitter|x\.com|facebook|linkedin|github|"
    r"discord|reddit|vk\.com|t\.me|telegram|threads|patreon|amazon|google)", re.IGNORECASE)


_LATIN = re.compile(r"[A-Za-z]")


def _normalize(text):
    """Чиним гомоглифы только в словах, где кириллица и латиница смешаны.

    Именно так выглядит маскировка ("БECПЛAТHЫЙ"). Целиком текст переводить нельзя:
    это ломает латинские URL внутри русского описания (t.me превращается в t.mе).
    """
    if not text:
        return ""
    out = []
    for token in re.split(r"(\s+)", text):
        if _CYRILLIC.search(token) and _LATIN.search(token):
            out.append(token.translate(_HOMOGLYPHS))
        else:
            out.append(token)
    return "".join(out)


def own_domain_signal(text):
    """Возвращает (вес, метка, домен) если в тексте есть ссылка на собственный сайт."""
    for host in _URL.findall(text or ""):
        if not _NOT_OWN_DOMAIN.search(host):
            return 14, "own-site-link", host
    return 0, None, None


def detect_lang(text):
    if not text:
        return None
    cyr = len(_CYRILLIC.findall(text))
    return "ru" if cyr >= max(5, len(text) * 0.05) else "en"


def leadgen_score(*texts):
    """Возвращает (score 0..100, [метки сигналов])."""
    blob = _normalize("\n".join(t for t in texts if t)).lower()
    if not blob:
        return 0.0, []
    raw, labels = 0.0, []
    for pattern, weight, label in SIGNALS + ANTI_SIGNALS:
        if re.search(pattern, blob, re.IGNORECASE):
            raw += weight
            labels.append(label)
    w, label, host = own_domain_signal(blob)
    if w:
        raw += w
        labels.append(f"{label}:{host}")
    score = max(0.0, min(100.0, raw / MAX_RAW * 100))
    return round(score, 1), labels


# --- классификация формата видео -------------------------------------------
FORMAT_RULES = [
    ("build-in-public", r"\b(i built|i made|built a|watch me build|building a|day \d+ of building)\b|"
                        r"(сделал (за|приложение|сайт)|собрал за|как я сделал)"),
    ("tool-demo",       r"\b(cursor|claude code|lovable|v0|bolt\.new|replit|windsurf|n8n|make\.com|supabase)\b"),
    ("before-after",    r"\b(before (and|vs\.?) after|redesign|i fixed|roast|teardown|review of your)\b|"
                        r"(до и после|редизайн|разбор сайта)"),
    ("money-proof",     r"\$[\d,.]+\s*(k|m|/mo|per month|a month)|\b(how i made|revenue|мл?н руб|тысяч рублей|заработал)\b"),
    ("client-work",     r"\b(client|freelance|agency|proposal|invoice|discovery call)\b|(клиент|фриланс|заказчик)"),
    ("tutorial",        r"\b(tutorial|how to|step by step|full course|guide)\b|(урок|как сделать|инструкция|туториал)"),
    ("opinion-hot-take",r"\b(why you|stop doing|the truth about|nobody talks about|unpopular opinion)\b|"
                        r"(почему нельзя|правда о|перестань)"),
]


def classify_format(title, description=None, duration=None):
    blob = " ".join(x for x in (title, description) if x).lower()
    tags = [name for name, pat in FORMAT_RULES if re.search(pat, blob, re.IGNORECASE)]
    if duration is not None:
        tags.append("short" if duration <= 90 else "long")
    return tags or (["short"] if (duration or 0) <= 90 else ["other"])


def traction(views_list, followers=None):
    """Медианные просмотры и отношение медианы к аудитории (насколько контент выходит за подписчиков)."""
    vals = [v for v in views_list if v]
    if not vals:
        return None, None, None
    med = int(statistics.median(vals))
    total = sum(vals)
    ratio = round(med / followers, 2) if followers else None
    return med, total, ratio
