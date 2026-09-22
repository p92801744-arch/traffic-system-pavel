from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from app.settings import settings


def load_sources() -> dict:
    return yaml.safe_load(settings.sources_path.read_text(encoding="utf-8")) or {}


def load_profile() -> dict:
    path: Path = settings.profile_path
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def configured_channels() -> list[dict]:
    sources = load_sources()
    items: list[dict] = []
    seen: set[str] = set()
    for channel in sources.get("channels") or []:
        username = (channel.get("username") or "").lstrip("@")
        if not username or username.lower() in seen:
            continue
        seen.add(username.lower())
        items.append({"username": username, "title": channel.get("title") or username})
    return items


def _plain(text: str) -> str:
    lowered = (text or "").lower().replace("ё", "е")
    return re.sub(r"[\s\u00a0\u2007\u202f]+", " ", lowered)


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """Фраза не считается найденной, если она кусок соседнего слова.

    «вакансии агент» — это не «ии агент».
    """
    lowered = _plain(text)
    found: list[str] = []
    for kw in keywords:
        phrase = _plain(kw)
        if phrase and re.search(r"(?<![0-9a-zа-яё])" + re.escape(phrase), lowered):
            found.append(kw)
    return found


def hits_stopword(text: str, stopwords: list[str]) -> str | None:
    lowered = _plain(text)
    for word in stopwords:
        if _plain(word) in lowered:
            return word
    return None


HELPER_JOB = re.compile(
    r"(помощниц\w*|помощник\w*|"
    r"ассистент\w*\s+ри[эе]лтор|"
    r"за смену|"
    r"размещени\w+\s+объявлен|"
    r"удал[её]нн\w+\s+работ|"
    r"ежедневн\w+\s+выплат|"
    r"гибкий график|"
    r"ведени\w+\s+(работы|магазин|кабинет|аккаунт)|"
    r"менеджер\w*\s+(wildberries|ozon|вб|маркетплейс)|"
    r"обработк\w+\s+входящих\s+заявок|"
    r"координаци\w+\s+дальнейшего|"
    r"полностью удал)",
    re.IGNORECASE,
)

SELF_PROMO = re.compile(
    r"(я\s+[а-яё]{2,20}\s*[-—–]\s*техническ|"
    r"закрываю всю техничку|"
    r"стоимость зависит от объ[её]ма|"
    r"пиши,?\s*расскажи что за задача|"
    r"технический специалист для|"
    r"в работе одновременно|"
    r"#помогу|мои услуг|обращайтесь|"
    r"возьму на себя)",
    re.IGNORECASE,
)

PRODUCT_AD = re.compile(
    r"(непростобот|neprostobot|"
    r"первые\s+\d+\s+дн\w+\s+бесплатно|"
    r"подключается к вашему аккаунту|"
    r"регистрация:\s*https?://)",
    re.IGNORECASE,
)

SEEK_WORD = re.compile(
    r"(?<![а-яё])("
    r"ищу|ищем|ищет|ищут|"
    r"ваканси[а-яё]*|"
    r"в поиске|"
    r"(?<!не )нужен|(?<!не )нужна|(?<!не )нужно|(?<!не )нужны|"
    r"требуется|требуются|"
    r"кто может|кто сделает"
    r")(?![а-яё])",
    re.IGNORECASE,
)

REAL_SERVICE = re.compile(
    r"(нейросет|chatgpt|\bgpt\b|\bn8n\b|автоматизац|"
    r"(?<![а-яё])ии[-\s]?ассистент|(?<![а-яё])ии[-\s]?агент|чат[-\s]?бот|"
    r"файнтюн|база знаний|разработ\w+\s+сайт|"
    r"карточки товар|карточки для)",
    re.IGNORECASE,
)


SLAG = re.compile(
    r"(весь список актуальных вакансий|яндекс\.?\s?еда|кладовщик|пеший курьер|"
    r"водитель-курьер|"
    r"нейросети делают|опыт не нужен|первые деньги можно получить|"
    r"рилсмейкер|монтаж[её]р|ретуш[её]р|smm-менеджер|смм-менеджер|"
    r"менеджер по продажам|таргетолог|тильдолог|маркетолог|"
    r"занимаюсь разработкой|чем могу быть полезн|"
    r"делаем сайты и веб-приложения|наше агентское портфолио|"
    r"дизайнер\w*|дизайн\w{0,6}\s+карточки|инфографик|пару слайдов|слайд|"
    r"оформлени\w+ карточки|визуал\w* карточки|"
    r"недвижимост|агент\w{0,6}\s+по\s+продаж|ри[эе]лтор|менеджерпопродажам|"
    r"рилс|сценарист|"
    r"обработк\w+\s+входящих\s+заявок|полностью удал)",
    re.IGNORECASE,
)

SPAM = re.compile(
    r"(подписывайся|переходи по ссылке|успей записаться|только сегодня|"
    r"бесплатный доступ|взаимопиар|"
    r"автоматически публикует объявления|"
    r"рассылк\w+ объявлен|"
    r"₽\s*/\s*мес|подписк\w+.{0,24}в месяц|далее\s*[—\-]\s*\d|"
    r"заработал\s*\+|пошаговой инструкц|уволиться из найма|"
    r"ищете работу|запустили telegram|вакансии без опыта|подработк)",
    re.IGNORECASE,
)

SYSTEM_BUILD = re.compile(
    r"(конвейер|автоматизац|\bn8n\b|\bmake\b|"
    r"генерац\w+\s+(описан|карточек)|чат[-\s]?бот|(?<![а-яё])ии[-\s]?агент|"
    r"файнтюн|база знаний|\b1с\b|\b1c\b)",
    re.IGNORECASE,
)


def text_fingerprint(text: str) -> str:
    """Один и тот же текст из разных чатов даёт один ключ."""
    lowered = (text or "").lower().replace("ё", "е")
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[@#][a-zа-я0-9_]+", " ", lowered)
    lowered = re.sub(r"[^a-zа-я0-9]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return hashlib.sha256(lowered.encode("utf-8")).hexdigest()[:32]


def is_spam(text: str) -> bool:
    text = _plain(text)
    if SPAM.search(text) or PRODUCT_AD.search(text):
        return True
    if len(re.findall(r"https?://", text)) >= 4:
        return True
    lines = [re.sub(r"\s+", " ", line).strip().lower() for line in text.splitlines()]
    lines = [line for line in lines if len(line) > 50]
    return len(lines) >= 3 and len(set(lines)) < len(lines)


def not_my_job(text: str) -> str | None:
    """Реклама, спам, живой помощник, самореклама или чужая вакансия."""
    text = _plain(text)
    if is_spam(text):
        return "реклама или спам"
    if SLAG.search(text) and not SYSTEM_BUILD.search(text):
        return "чужая вакансия или реклама"
    if HELPER_JOB.search(text) and not REAL_SERVICE.search(text):
        return "ищут живого помощника, не услугу"
    if SELF_PROMO.search(text) and not re.search(r"(?<![а-яё])(ищу|ищем|требует|ваканси)", text, re.IGNORECASE):
        return "исполнитель рекламирует себя"
    return None


def intake_hits(text: str, sources: dict) -> list[str] | None:
    """Проходит только «ищу / требуется» вместе с ключом услуги. Реклама и помощники — нет."""
    if hits_stopword(text, sources.get("stopwords") or []):
        return None
    if not_my_job(text):
        return None
    if not SEEK_WORD.search(text):
        return None
    hits = match_keywords(text, sources.get("keywords") or [])
    if not hits:
        return None
    return hits
