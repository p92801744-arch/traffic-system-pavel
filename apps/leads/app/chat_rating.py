"""Правила чата, спам-оценка, рейтинг и черновик визитки. Сеть сюда не входит."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from app.settings import settings

STATUSES = ("active", "no_ads", "spam_drop", "unknown")
STATUS_LABELS = {
    "active": "можно визитку",
    "no_ads": "реклама запрещена",
    "spam_drop": "спам",
    "unknown": "неясно",
}

SPAM_DROP_SCORE = 0.55
SPAM_MIN_MESSAGES = 25
CYCLE_DAYS = 10
CYCLE_EPOCH = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)

_DAY_PATTERNS = (
    (re.compile(r"понедельник\w*|\bпн\b"), "пн"),
    (re.compile(r"вторник\w*|\bвт\b"), "вт"),
    (re.compile(r"сред\w*|\bср\b"), "ср"),
    (re.compile(r"четверг\w*|\bчт\b"), "чт"),
    (re.compile(r"пятниц\w*|\bпт\b"), "пт"),
    (re.compile(r"суббот\w*|\bсб\b"), "сб"),
    (re.compile(r"воскресень\w*|\bвс\b"), "вс"),
)

_FORBID_ADS = (
    r"реклам\w* запрещ",
    r"запрещ\w* реклам",
    r"без реклам",
    r"не для реклам",
    r"реклам\w* не (?:размещ|публик|допуска)",
    r"нельзя реклам",
    r"рекламу нельзя",
    r"рекламн\w+ (?:сообщен|пост|запис)\w* (?:удаля|запрещ|баня)",
    r"за реклам\w* бан",
    r"бан за реклам",
)
_ALLOW_ADS = (
    r"реклам\w* разреш",
    r"разреш\w* реклам",
    r"можно реклам",
    r"реклам\w* можно",
    r"реклам\w* приветств",
    r"рекламн\w+ день",
    r"день реклам",
)
_FORBID_CARDS = (
    r"визитк\w* запрещ",
    r"запрещ\w* визитк",
    r"без визиток",
    r"визитки нельзя",
    r"нельзя визитк",
)
_ALLOW_CARDS = (
    r"визитк\w* разреш",
    r"разреш\w* визитк",
    r"можно визитк",
    r"визитк\w* можно",
    r"визитк\w* приветств",
    r"размещени\w* визиток",
    r"визитки по ",
    r"визитка по ",
)
_FORBID_DM = (
    r"личк\w* запрещ",
    r"в личку не писа",
    r"не писать в лич",
    r"не писать в лс",
    r"лс запрещ",
    r"в лс нельзя",
    r"личн\w+ сообщени\w* запрещ",
    r"запрещ\w* (?:писать )?в (?:лс|личк)",
    r"без (?:спама в )?личк",
    r"\bdm запрещ",
)
_ALLOW_DM = (
    r"можно в личк",
    r"пишите в личк",
    r"в лс можно",
    r"личк\w* разреш",
)

_AD_POST = re.compile(
    r"(?i)(#реклама|#визитка|#услуги|\bвизитка\b|мои услуги|предлагаю услуги|"
    r"\bсделаю\b|\bвыполню\b|\bпрайс\b|подписывайтесь|беру заказы|"
    r"открыт[аы]? к сотрудничеству|пишите в лс|пишите в личку|"
    r"я (?:дизайнер|таргетолог|смм|smm|копирайтер|маркетолог))"
)
_SEEK_POST = re.compile(
    r"(?i)(\bищу\b|\bищем\b|\bнужен\b|\bнужна\b|\bнужны\b|\bтребуется\b|\bвакансия\b)"
)
_RULEISH = re.compile(
    r"реклам|визитк|хэштег|хештег|ссылк|личк|\bлс\b|запрещ|разреш"
)

_HASH_N = 32
_NEAR = 0.75


def chat_key(username: str | None, channel_id) -> str:
    name = (username or "").lstrip("@").strip().lower()
    if name:
        return name
    if channel_id is None or str(channel_id).strip() == "":
        return ""
    return f"id-{int(channel_id)}"


def source_chats(sources: dict) -> list[dict]:
    """Все чаты из sources.yaml, включая кандидатов и чаты только с id."""
    found: list[dict] = []
    seen: set[str] = set()
    for channel in sources.get("channels") or []:
        username = (channel.get("username") or "").lstrip("@").strip()
        key = chat_key(username, channel.get("channel_id"))
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "chat_key": key,
                "username": username,
                "channel_id": channel.get("channel_id"),
                "title": (channel.get("title") or username or key).strip(),
                "invite": (channel.get("invite") or "").strip(),
                "join": (channel.get("join") or "").strip().lower(),
            }
        )
    return found


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _hits(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _tri(forbid: bool, allow: bool) -> tuple[bool | None, bool]:
    """Значение и флаг конфликта."""
    if forbid and allow:
        return None, True
    if forbid:
        return False, False
    if allow:
        return True, False
    return None, False


def _hashtag(original: str) -> str | None:
    patterns = (
        r"(?:хэштег\w*|хештег\w*|хэш-тег\w*|хеш-тег\w*)\s*[:\-]?\s*(#[\w\d_]{2,})",
        r"обязательн\w*\s+(?:с\s+)?(#[\w\d_]{2,})",
        r"(#[\w\d_]{2,})\s*(?:обязател|в начале|в конце)",
    )
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _days(text: str) -> list[str]:
    found: list[str] = []
    for pattern, label in _DAY_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 80) : match.end() + 80]
            if re.search(r"реклам|визитк|публик|размещ|пост", window):
                if label not in found:
                    found.append(label)
    return found


def _frequency(text: str) -> str | None:
    patterns = (
        r"не чаще(?: чем)? [^.\n]{0,48}",
        r"(?:один раз|1 раз|раз) в (?:\d+\s+)?(?:день|дня|дней|сутки|недел\w*|месяц\w*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip(" .,-")
    return None


def _count_limit(text: str, noun: str) -> int | None:
    if re.search(rf"без {noun}|{noun}\w* запрещ|запрещ\w* {noun}|нельзя {noun}", text):
        return 0
    numbered = re.search(rf"не более (\d+) {noun}", text)
    if numbered:
        return int(numbered.group(1))
    if re.search(rf"(?:одна|одной|один|1) {noun}", text):
        return 1
    return None


def _format_line(original: str) -> str | None:
    match = re.search(
        r"(?:формат|шаблон)\s*[:\-—]\s*([^\n]{3,160})",
        original or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip(" .")


def extract_rules(description: str, pinned: str) -> dict:
    """Сначала закреп и описание, без модели. Неясный текст помечается needs_llm."""
    original = "\n\n".join(part for part in (pinned or "", description or "") if part)
    text = _norm(original)
    ads, ads_conflict = _tri(_hits(text, _FORBID_ADS), _hits(text, _ALLOW_ADS))
    cards, cards_conflict = _tri(_hits(text, _FORBID_CARDS), _hits(text, _ALLOW_CARDS))
    dm_allowed, dm_conflict = _tri(_hits(text, _FORBID_DM), _hits(text, _ALLOW_DM))
    if dm_allowed is False:
        dm_value: bool | None = True
    elif dm_allowed is True:
        dm_value = False
    else:
        dm_value = None
    conflict = ads_conflict or cards_conflict or dm_conflict
    ruleish = bool(_RULEISH.search(text))
    settled = ads is not None or cards is not None
    needs_llm = bool(text.strip()) and (conflict or (ruleish and not settled))
    return {
        "ads_allowed": ads,
        "cards_allowed": cards,
        "required_hashtag": _hashtag(original),
        "required_format": _format_line(original),
        "allowed_days": _days(text),
        "link_limit": _count_limit(text, "ссыл"),
        "contact_limit": _count_limit(text, "контакт"),
        "max_frequency": _frequency(text),
        "dm_banned": dm_value,
        "confident": not needs_llm,
        "conflict": conflict,
        "needs_llm": needs_llm,
        "llm_used": False,
    }


def merge_llm_rules(base: dict, extra: dict) -> dict:
    """Модель дополняет пустые поля. При конфликте эвристик её ответ их заменяет."""
    merged = dict(base)
    fields = (
        "ads_allowed",
        "cards_allowed",
        "required_hashtag",
        "required_format",
        "link_limit",
        "contact_limit",
        "max_frequency",
        "dm_banned",
    )
    for field in fields:
        if field not in extra or extra.get(field) is None:
            continue
        if merged.get("conflict") or merged.get(field) is None:
            merged[field] = extra[field]
    days = extra.get("allowed_days")
    if isinstance(days, list) and days and not merged.get("allowed_days"):
        merged["allowed_days"] = [str(day) for day in days]
    merged["llm_used"] = True
    merged["needs_llm"] = False
    merged["confident"] = True
    merged["conflict"] = False
    return merged


def llm_requested(rules: dict) -> bool:
    return bool(rules.get("needs_llm")) and not rules.get("llm_used")


async def clarify_rules(rules: dict, description: str, pinned: str, post=None) -> dict:
    """LLM только если эвристика не уверена и в окружении есть LLM_API_KEY."""
    if not llm_requested(rules):
        return rules
    key = (settings.llm_api_key or "").strip()
    if not key:
        return rules
    original = "\n\n".join(part for part in (pinned or "", description or "") if part)[:6000]
    payload = {
        "model": settings.llm_model or "deepseek-chat",
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Извлеки правила рекламы и визиток из описания чата Telegram. "
                    "Ответь одним JSON-объектом с полями ads_allowed, cards_allowed "
                    "(true/false/null), required_hashtag, required_format, allowed_days "
                    "(массив коротких дней пн..вс), link_limit, contact_limit "
                    "(число или null), max_frequency (строка или null), dm_banned "
                    "(true/false/null). Не выдумывай запрет, если его нет в тексте."
                ),
            },
            {"role": "user", "content": original or "текста нет"},
        ],
    }
    url = (settings.llm_base_url or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    if post is None:
        import httpx

        async def post(target, body, headers):
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.post(target, json=body, headers=headers)
                response.raise_for_status()
                return response.json()

    try:
        data = await post(
            url,
            payload,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(_json_blob(content))
    except Exception:
        return rules
    if not isinstance(parsed, dict):
        return rules
    return merge_llm_rules(rules, _clean_llm(parsed))


def _json_blob(content: str) -> str:
    text = (content or "").strip()
    fence = re.search(r"\{.*\}", text, flags=re.DOTALL)
    return fence.group(0) if fence else text


def _clean_llm(raw: dict) -> dict:
    def flag(value):
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        lowered = str(value).strip().lower()
        if lowered in {"true", "yes", "да", "1"}:
            return True
        if lowered in {"false", "no", "нет", "0"}:
            return False
        return None

    def limit(value):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    days = raw.get("allowed_days") or []
    if isinstance(days, str):
        days = [part.strip() for part in days.split(",") if part.strip()]
    return {
        "ads_allowed": flag(raw.get("ads_allowed")),
        "cards_allowed": flag(raw.get("cards_allowed")),
        "required_hashtag": (raw.get("required_hashtag") or None) or None,
        "required_format": (raw.get("required_format") or None) or None,
        "allowed_days": list(days) if isinstance(days, list) else [],
        "link_limit": limit(raw.get("link_limit")),
        "contact_limit": limit(raw.get("contact_limit")),
        "max_frequency": (str(raw["max_frequency"]).strip() if raw.get("max_frequency") else None),
        "dm_banned": flag(raw.get("dm_banned")),
    }


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]{2,}", _norm(text))


def word_shingles(text: str, size: int = 3) -> set[str]:
    words = _tokens(text)
    if not words:
        return set()
    if len(words) <= size:
        return {" ".join(words)}
    return {" ".join(words[index : index + size]) for index in range(len(words) - size + 1)}


def minhash_signature(shingles: set[str], count: int = _HASH_N) -> tuple[int, ...]:
    if not shingles:
        return tuple()
    signature = [2**32 - 1] * count
    for shingle in shingles:
        digest = hashlib.md5(shingle.encode("utf-8")).digest()
        first = int.from_bytes(digest[:4], "little")
        second = int.from_bytes(digest[4:8], "little") or 1
        for index in range(count):
            value = (first + index * second) & 0xFFFFFFFF
            if value < signature[index]:
                signature[index] = value
    return tuple(signature)


def signature_similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    same = sum(1 for a, b in zip(left, right) if a == b)
    return same / len(left)


def _signatures(texts: list[str]) -> list[tuple[int, ...]]:
    return [minhash_signature(word_shingles(text)) for text in texts]


def _near(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    return signature_similarity(left, right) >= _NEAR


def is_ad_post(text: str) -> bool:
    if _SEEK_POST.search(text or "") and not re.search(r"(?i)#реклама|#визитка", text or ""):
        return False
    return bool(_AD_POST.search(text or ""))


def spam_metrics(messages: list[dict]) -> dict:
    """Доли по последней выборке. Пустые служебные сообщения не считаются дублями."""
    textual = [item for item in messages if (item.get("text") or "").strip()]
    texts = [item.get("text") or "" for item in textual]
    signatures = _signatures(texts)
    duplicates = 0
    for index, signature in enumerate(signatures):
        if any(_near(signature, signatures[earlier]) for earlier in range(index)):
            duplicates += 1
    reposts = 0
    by_sender: dict[str, list[int]] = {}
    for index, item in enumerate(textual):
        sender = str(item.get("sender") or "")
        by_sender.setdefault(sender, []).append(index)
    for indexes in by_sender.values():
        if len(indexes) < 2:
            continue
        for position, index in enumerate(indexes):
            earlier = indexes[:position]
            if any(_near(signatures[index], signatures[other]) for other in earlier):
                reposts += 1
    total_text = len(textual)
    ads = sum(1 for text in texts if is_ad_post(text))
    bots = sum(1 for item in messages if item.get("bot"))
    total = len(messages)
    duplicate_share = duplicates / total_text if total_text else 0.0
    repost_share = reposts / total_text if total_text else 0.0
    ad_share = ads / total_text if total_text else 0.0
    bot_share = bots / total if total else 0.0
    per_day = _messages_per_day(messages)
    velocity = min(1.0, per_day / 300.0)
    score = (
        0.34 * duplicate_share
        + 0.22 * repost_share
        + 0.28 * ad_share
        + 0.08 * velocity
        + 0.08 * bot_share
    )
    return {
        "messages_collected": total,
        "duplicate_share": round(duplicate_share, 4),
        "repost_share": round(repost_share, 4),
        "ad_share": round(ad_share, 4),
        "bot_share": round(bot_share, 4),
        "messages_per_day": round(per_day, 2),
        "spam_score": round(min(1.0, score), 4),
    }


def _messages_per_day(messages: list[dict]) -> float:
    dates = [item["date"] for item in messages if isinstance(item.get("date"), datetime)]
    count = len(messages)
    if count == 0:
        return 0.0
    if len(dates) < 2:
        return float(count)
    span_days = (max(dates) - min(dates)).total_seconds() / 86400
    if span_days < 1:
        span_days = 1.0
    return count / span_days


def write_permission(entity, perms) -> tuple[bool, bool, str]:
    """member, can_write, короткая причина. entity и perms — утки, не обязательно Telethon."""
    if perms is None or getattr(perms, "has_left", False):
        return False, False, "не состоим в чате"
    if getattr(perms, "is_banned", False):
        rights = getattr(getattr(perms, "participant", None), "banned_rights", None)
        blocked = bool(getattr(rights, "send_messages", True))
        if blocked:
            return True, False, "на сообщения стоит запрет"
        return True, True, ""
    broadcast = bool(getattr(entity, "broadcast", False)) and not bool(getattr(entity, "megagroup", False))
    if broadcast and not (getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False)):
        return True, False, "это канал, писать могут только админы"
    if getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False):
        return True, True, ""
    default_rights = getattr(entity, "default_banned_rights", None)
    if default_rights is not None and getattr(default_rights, "send_messages", False):
        return True, False, "в чате закрыта отправка сообщений"
    return True, True, ""


def compute_rating(rules: dict, spam: dict, leads: dict, *, member: bool | None, can_write: bool | None) -> int:
    """0–100. Выше — ближе к визитке. Запрет и спам снижают, живые заявки поднимают."""
    score = 40
    if rules.get("cards_allowed") is True:
        score += 25
    elif rules.get("ads_allowed") is True:
        score += 12
    if rules.get("cards_allowed") is False or (
        rules.get("ads_allowed") is False and rules.get("cards_allowed") is not True
    ):
        score -= 35
    score -= int(float(spam.get("spam_score") or 0) * 40)
    if can_write is True:
        score += 10
    elif can_write is False:
        score -= 15
    if member is False:
        score -= 10
    good = int(leads.get("fit3") or 0)
    rejected = int(leads.get("rejected") or 0)
    score += min(good * 3, 15)
    if rejected > good:
        score -= min(10, rejected - good)
    return max(0, min(100, score))


def decide_status(
    rules: dict,
    spam: dict,
    *,
    member: bool | None,
    can_write: bool | None,
    read_error: str = "",
    description: str = "",
    pinned: str = "",
    leads: dict | None = None,
    write_note: str = "",
) -> tuple[str, str]:
    leads = leads or {}
    has_text = bool((description or "").strip() or (pinned or "").strip())
    if read_error and not has_text and not spam.get("messages_collected"):
        return "unknown", read_error
    cards = rules.get("cards_allowed")
    ads = rules.get("ads_allowed")
    if cards is False or (ads is False and cards is not True):
        reason = "В закреплённом сообщении или в описании реклама или визитки запрещены."
        return "no_ads", _with_leads(reason, leads)
    enough = int(spam.get("messages_collected") or 0) >= SPAM_MIN_MESSAGES
    if enough and float(spam.get("spam_score") or 0) >= SPAM_DROP_SCORE:
        reason = (
            "Слишком много одинаковых текстов, повторных отправителей или саморекламы. "
            "Визитка здесь потеряется."
        )
        return "spam_drop", _with_leads(reason, leads)
    if member is False:
        return "unknown", _with_leads("В чате не состоим, поэтому не видно, можно ли писать.", leads)
    if can_write is False:
        note = (write_note or "").strip() or "Нет права писать."
        return "unknown", _with_leads(note, leads)
    if cards is True or ads is True:
        if member is True and can_write is True:
            reason = "Визитку писать можно: правила не запрещают, право писать есть, спама не больше порога."
            return "active", _with_leads(reason, leads)
        return "unknown", _with_leads(
            "Правила разрешают визитку, но право писать не подтверждено.",
            leads,
        )
    if rules.get("needs_llm"):
        return "unknown", _with_leads(
            "В правилах есть слова про рекламу, но разрешение неясно. Ключ модели не использовался или не помог.",
            leads,
        )
    return "unknown", _with_leads(
        "Явного разрешения на визитку в правилах нет. Сам такой чат не ставлю в план.",
        leads,
    )


def _with_leads(reason: str, leads: dict) -> str:
    good = int(leads.get("fit3") or 0)
    rejected = int(leads.get("rejected") or 0)
    extra = []
    if good:
        extra.append(f"Заявок с оценкой 3 и выше: {good}.")
    if rejected:
        extra.append(f"Отказов: {rejected}.")
    if not extra:
        return reason
    return reason + " " + " ".join(extra)


def plan_slots(chat_keys: list[str], now: datetime) -> dict[str, datetime]:
    """Одна визитка на чат в 10 дней. Активные чаты разложены равномерно по циклу."""
    ordered = sorted({key for key in chat_keys if key})
    count = len(ordered)
    if count == 0:
        return {}
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    span = timedelta(days=CYCLE_DAYS)
    step = span / count
    elapsed = (now - CYCLE_EPOCH).total_seconds()
    cycles = int(elapsed // span.total_seconds())
    if elapsed < 0:
        cycles -= 1
    cycle_start = CYCLE_EPOCH + cycles * span
    slots: dict[str, datetime] = {}
    for index, key in enumerate(ordered):
        when = cycle_start + step * index
        if when <= now:
            when += span
        slots[key] = when
    return slots


def rules_summary(rules: dict) -> str:
    parts: list[str] = []
    cards = rules.get("cards_allowed")
    ads = rules.get("ads_allowed")
    if cards is True:
        parts.append("визитки можно")
    elif cards is False:
        parts.append("визитки нельзя")
    if ads is True:
        parts.append("реклама можно")
    elif ads is False:
        parts.append("реклама нельзя")
    if rules.get("required_hashtag"):
        parts.append(str(rules["required_hashtag"]))
    days = rules.get("allowed_days") or []
    if days:
        parts.append("дни: " + ", ".join(days))
    if rules.get("link_limit") is not None:
        parts.append(f"ссылок: {rules['link_limit']}")
    if rules.get("contact_limit") is not None:
        parts.append(f"контактов: {rules['contact_limit']}")
    if rules.get("max_frequency"):
        parts.append(str(rules["max_frequency"]))
    if rules.get("dm_banned") is True:
        parts.append("личка запрещена")
    elif rules.get("dm_banned") is False:
        parts.append("личка можно")
    if rules.get("required_format"):
        parts.append("есть формат")
    if not parts:
        return "явных правил нет"
    return " · ".join(parts)


def draft_card(title: str, rules: dict) -> dict:
    """Черновик под правила чата. Никуда не отправляется."""
    cards = rules.get("cards_allowed")
    ads = rules.get("ads_allowed")
    if cards is False or (ads is False and cards is not True):
        return {
            "text": "",
            "note": "Черновик не собран: в правилах запрещена реклама или визитка. Отправлять нечего.",
        }

    notes: list[str] = []
    name = (title or "этот чат").strip()
    link_limit = rules.get("link_limit")
    contact_limit = rules.get("contact_limit")
    dm_banned = rules.get("dm_banned") is True
    if dm_banned:
        ask = "Если задача похожа, можно ответить прямо в чате: что уже есть и где сейчас ручная работа."
    else:
        ask = "Если задача похожа — напишите, что уже есть и где сейчас ручная работа. С этого и начнём."
    paragraphs = [
        "Здравствуйте! Я Павел Клюшин, технический партнёр для бизнеса.",
        _angle(name),
        ask,
    ]
    contacts: list[str] = []
    if link_limit != 0:
        contacts.append("Сайт https://pavelklyushin.ru/")
    if not dm_banned:
        contacts.append("Telegram @pasha_kvk")
    if isinstance(contact_limit, int):
        contacts = contacts[: max(contact_limit, 0)]
    if contacts:
        paragraphs.append(" ".join(contacts))
    if link_limit == 0 and dm_banned:
        notes.append("Ссылку и увод в личку не ставлю: так написано в правилах.")

    tag = (rules.get("required_hashtag") or "").strip()
    if tag and not tag.startswith("#"):
        tag = "#" + tag
    fmt = (rules.get("required_format") or "").strip()
    if fmt:
        paragraphs.insert(0, fmt)
        notes.append(f"В правилах указан формат: {fmt}.")
    days = rules.get("allowed_days") or []
    if days:
        notes.append("Публиковать только в эти дни: " + ", ".join(str(day) for day in days) + ".")
    if rules.get("max_frequency"):
        notes.append(
            f"Ограничение чата: {rules['max_frequency']}. План ящика всё равно не чаще одного раза в 10 дней."
        )
    if link_limit == 0:
        notes.append("Ссылок в тексте нет.")
    if dm_banned:
        notes.append("Призыва писать в личку нет.")
    if tag:
        notes.append(f"Хэштег чата: {tag}.")

    text = "\n\n".join(paragraphs)
    if link_limit == 0:
        text = re.sub(r"https?://\S+", "", text)
    if dm_banned:
        text = re.sub(r"(?i)напишите в личк\w*|в личку|в лс", "", text)
        text = text.replace("@pasha_kvk", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if tag and tag.lower() not in text.lower():
        text = f"{text}\n\n{tag}".strip()
    return {"text": text, "note": " ".join(notes).strip()}


def _angle(title: str) -> str:
    lowered = title.lower().replace("ё", "е")
    if any(word in lowered for word in ("wildberries", "ozon", "вайлдберриз", "озон", "маркетплейс", "магазин", "каталог", "аккумулятор", " wb", "wb ")):
        detail = (
            "С магазинами работаю через карточки и каталог: характеристики, описания, выгрузка. "
            "Кабинет селлера не веду и один слайд не рисую. "
            "По устройству могу показать интернет-магазин аккумуляторов, без названия и без витрины."
        )
    elif any(word in lowered for word in ("n8n", "make", "zapier", "автомат")):
        detail = (
            "Собираю сценарии на n8n и Make: заявка из мессенджера попадает в CRM или таблицу, "
            "спорное остаётся человеку."
        )
    elif any(word in lowered for word in ("1с", "1c", "мойсклад", "моиссклад", "склад")):
        detail = "Связываю сайт, 1С и склад, чтобы заказы не переносили руками."
    elif "дизайн" in lowered:
        detail = "Сам дизайн одной карточки не рисую. Беру конвейер: характеристики, тексты и выгрузка каталога."
    elif any(word in lowered for word in ("сайт", "tilda", "тильд", "wordpress")):
        detail = "Делаю сайты и живые каталоги под задачу, а не правку баннера на вечер."
    else:
        detail = "Собираю сайты, живые каталоги и связки сервисов. Сначала задача, потом инструмент."
    return f"В «{title}» пишу по делу. {detail}"


def rules_from_row(row: dict) -> dict:
    def flag(value):
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        return bool(int(value))

    days = row.get("allowed_days") or []
    if isinstance(days, str):
        days = [part for part in days.split(",") if part]
    return {
        "ads_allowed": flag(row.get("ads_allowed")),
        "cards_allowed": flag(row.get("cards_allowed")),
        "required_hashtag": row.get("required_hashtag") or None,
        "required_format": row.get("required_format") or None,
        "allowed_days": list(days),
        "link_limit": row.get("link_limit"),
        "contact_limit": row.get("contact_limit"),
        "max_frequency": row.get("max_frequency") or None,
        "dm_banned": flag(row.get("dm_banned")),
        "needs_llm": bool(row.get("needs_llm")),
        "confident": bool(row.get("confident")),
    }


def card_plan_updates(rows: list[dict], now: datetime) -> list[dict]:
    active = [row["chat_key"] for row in rows if effective_status(row) == "active"]
    slots = plan_slots(active, now)
    updates: list[dict] = []
    for row in rows:
        rules = rules_from_row(row)
        draft = draft_card(row.get("title") or "", rules)
        when = slots.get(row["chat_key"])
        updates.append(
            {
                "chat_key": row["chat_key"],
                "next_card_at": when.isoformat(timespec="seconds") if when else None,
                "card_draft": draft["text"],
                "card_draft_note": draft["note"],
            }
        )
    return updates


async def refresh_card_plan(conn, now: datetime | None = None) -> None:
    from app import db

    moment = now or datetime.now(timezone.utc)
    rows = await db.list_chat_profiles(conn, "chat", "asc")
    await db.write_card_plan(conn, card_plan_updates(rows, moment))


def effective_status(row: dict) -> str:
    manual = (row.get("status_override") or "").strip()
    if manual in STATUSES:
        return manual
    auto = (row.get("status_auto") or "").strip()
    return auto if auto in STATUSES else "unknown"


def display_reason(row: dict) -> str:
    auto = row.get("status_reason") or ""
    manual = (row.get("status_override") or "").strip()
    if manual in STATUSES:
        label = STATUS_LABELS.get(manual, manual)
        return f"Статус поставлен вручную: {label}. Автооценка: {auto}"
    return auto
