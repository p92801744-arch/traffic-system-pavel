from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from app.lessons import learned_block
from app.settings import settings


def load_sources() -> dict:
    return yaml.safe_load(settings.sources_path.read_text(encoding="utf-8")) or {}


def load_profile() -> dict:
    path: Path = settings.profile_path
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _is_join_candidate(channel: dict) -> bool:
    return str(channel.get("join") or "").strip().lower() == "candidate"


def configured_channels() -> list[dict]:
    """Чаты, которые уже слушаем. Кандидаты, куда Павел вступает сам, сюда не входят."""
    sources = load_sources()
    items: list[dict] = []
    seen: set[str] = set()
    for channel in sources.get("channels") or []:
        if _is_join_candidate(channel):
            continue
        username = (channel.get("username") or "").lstrip("@")
        if not username or username.lower() in seen:
            continue
        seen.add(username.lower())
        items.append({"username": username, "title": channel.get("title") or username})
    return items


def candidate_channels() -> list[dict]:
    """Чаты, куда Павел вступает сам. Сборщик их не открывает и не входит."""
    sources = load_sources()
    items: list[dict] = []
    seen: set[str] = set()
    for channel in sources.get("channels") or []:
        if not _is_join_candidate(channel):
            continue
        username = (channel.get("username") or "").lstrip("@")
        if not username or username.lower() in seen:
            continue
        seen.add(username.lower())
        items.append(
            {
                "username": username,
                "title": channel.get("title") or username,
                "why": (channel.get("why") or "").strip(),
                "url": f"https://t.me/{username}",
            }
        )
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
        if phrase and re.search(r"(?<![0-9a-zа-я])" + re.escape(phrase), lowered):
            found.append(kw)
    return found


def hits_stopword(text: str, stopwords: list[str]) -> str | None:
    """Стоп-фраза с левой границей слова: «крипт» не срабатывает внутри «скрипт»."""
    lowered = _plain(text)
    for word in stopwords:
        phrase = _plain(word)
        if phrase and re.search(r"(?<![0-9a-zа-я])" + re.escape(phrase), lowered):
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

# Отдельно: «ozon/wb» в REAL_SERVICE раньше спасало такие вакансии.
MARKETPLACE_MANAGER = re.compile(
    r"("
    r"менеджер\w*.{0,48}(маркетплейс|wildberries|\bozon\b|\bозон\b|\bвб\b|\bwb\b)|"
    r"(маркетплейс|wildberries|\bozon\b|\bозон\b|\bвб\b|\bwb\b).{0,48}менеджер|"
    r"менеджер[-\s]?аналитик|"
    r"помощник\w*.{0,24}(по\s+)?(ozon|озон|wildberries|вб|wb|маркетплейс)|"
    r"ведени\w+\s+(работы\s+с\s+)?(кабинет|магазин).{0,40}(wildberries|\bozon\b|озон|вб|wb|маркетплейс)|"
    r"кабинет(ом|а)?\s+(wildberries|\bozon\b|озон|вб|wb)|"
    r"какого менеджера ищу|"
    r"менеджер\w*\s+по\s+(ozon|озон|wildberries|вб|wb|маркетплейс)"
    r")",
    re.IGNORECASE,
)

MARKETPLACE_TOPIC = re.compile(
    r"(маркетплейс|wildberries|вайлдберр?\w*|\bozon\b|\bозон\b|(?<![а-я])вб(?![а-я])|\bwb\b)",
    re.IGNORECASE,
)

# Единственное, что оставляем из темы маркетплейсов.
PRODUCT_CARDS = re.compile(
    r"("
    r"карточк\w+\s+товар|"
    r"товарн\w+\s+карточ|"
    r"созда(ть|ни[ея]|вать|ём|ем)\s+карточ|"
    r"генерац\w+\s+(карточ|описан)|"
    r"конвейер\w*\s+карточ|"
    r"контент[-\s]?конвейер|"
    r"контент[-\s]?завод|"
    r"описани\w+\s+товар|"
    r"карточк\w+\s+(для|на)\s+(wildberries|ozon|озон|вб|wb|маркетплейс)|"
    r"заполн\w+\s+карточ"
    r")",
    re.IGNORECASE,
)

SELF_PROMO = re.compile(
    r"(я\s+[а-я]{2,20}\s*[-—–]\s*техническ|"
    r"закрываю всю техничку|"
    r"стоимость зависит от объ[её]ма|"
    r"пиши,?\s*расскажи что за задача|"
    r"технический специалист для|"
    r"в работе одновременно|"
    r"#помогу|мои услуг|обращайтесь|"
    r"возьму на себя)",
    re.IGNORECASE,
)

# Визитка и самореклама. Не снимается словом «ищут» / «ищете» и словом «сайт».
SELF_PROMO_STRONG = re.compile(
    r"(меня зовут|"
    r"я\s*[—–-]\s*[a-zа-я]|"
    r"я\s+(разработчик|специалист|эксперт|делаю|создаю|помогаю|занимаюсь)|"
    r"мои (работы|кейсы|проекты)|"
    r"портфолио|кейсы в профиле|"
    r"знаю человека|могу дать контакт|"
    r"пишите в (лс|личку)|пиши(те)? в (лс|личку)|"
    r"наши работы|показываем наши|"
    r"помогу создать|работаю на бизнес|планируете веб|"
    r"ищу партн[её]р|"
    r"\d+\+?\s*(лет|года)\s+(в|опыта)|"
    r"берусь за|принимаю заказы|свободен для проектов|"
    r"#помогу|мои услуг|возьму на себя|"
    r"занимаюсь разработкой|чем могу быть полез|"
    r"делаем сайты и веб-приложения|наше агентское портфолио)",
    re.IGNORECASE,
)

BUSINESS_CARD = re.compile(
    r"(у вас хороший продукт|"
    r"хотите,? чтобы клиенты|"
    r"полезно иметь второе мнение|"
    r"знаю человека|могу дать контакт|"
    r"визитк\w+\s+(человек|специалист)|"
    r"открыт[аы]?\s+к\s+сотрудничеству|"
    r"принимаю заказы|беру заказы)",
    re.IGNORECASE,
)

PRODUCT_AD = re.compile(
    r"(непростобот|neprostobot|"
    r"первые\s+\d+\s+дн\w+\s+бесплатно|"
    r"подключается к вашему аккаунту|"
    r"регистрация:\s*https?://|"
    r"готовые клиенты|ищут ваши услуги|перехват лидов|"
    r"лиды,?\s+которые конвертир|"
    r"сервис автоматическ|перенеси\.ру)",
    re.IGNORECASE,
)

REVIEW_CASE = re.compile(
    r"("
    r"(?<![а-я])отзыв(?![а-я])|"
    r"спасибо за (работу|проект|бот)|"
    r"(?<![а-я])рекомендую(?![а-я])|"
    r"сделали для (клиент|заказчик)|"
    r"наш кейс|(?<![а-я])кейс:|"
    r"было\s*[—:-].{0,40}стало|"
    r"до и после|"
    r"результат:\s*[+\-]?\d"
    r")",
    re.IGNORECASE,
)

# Запрос заказчика в начале поста. «Ищут» и «ищете» сюда не входят.
CLIENT_OPEN = re.compile(
    r"(?<![а-я])("
    r"(ищу|ищем)\s+(исполнител\w*|разработчик\w*|специалист\w*|подрядчик\w*|человек\w*|фрилансер\w*|кого-нибудь)|"
    r"(нужен|нужна|нужны|требуется|требуются)\s+"
    r"(исполнител\w*|разработчик\w*|специалист\w*|подрядчик\w*|человек\w*|фрилансер\w*|"
    r"бот\w*|сайт\w*|агент\w*|интеграц\w*|автоматизац\w*|связк\w*|систем\w*)"
    r")(?![а-я])",
    re.IGNORECASE,
)

CLIENT_INTENT = re.compile(
    r"("
    r"(?<![а-я])(ищу|ищем)\s+(исполнител\w*|разработчик\w*|специалист\w*|подрядчик\w*|человек\w*|фрилансер\w*|кого-нибудь)|"
    r"(?<![а-я])(нужен|нужна|нужны|требуется|требуются)\s+"
    r"(исполнител\w*|разработчик\w*|специалист\w*|подрядчик\w*|человек\w*|фрилансер\w*|"
    r"бот\w*|сайт\w*|агент\w*|интеграц\w*|автоматизац\w*|связк\w*|систем\w*)|"
    r"(?<![а-я])(бюджет|оплачу|готовы оплатить|техническ\w+\s+задани\w*)(?![а-я])"
    r")",
    re.IGNORECASE,
)

SEEK_WORD = re.compile(
    r"(?<![а-я])("
    r"ищу|ищем|ищет|ищут|"
    r"ваканси[а-я]*|"
    r"(?<!не )нужен|(?<!не )нужна|(?<!не )нужны|"
    r"нужно\s+(?:сделать|разработать|настроить|собрать|автоматизир\w*|внедрить|связать)|"
    r"требуется|требуются|"
    r"кто может|кто сделает"
    r")(?![а-я])",
    re.IGNORECASE,
)

REAL_SERVICE = re.compile(
    r"(нейросет|chatgpt|\bgpt\b|\bn8n\b|автоматизац|make\.com|\bmake\b|"
    r"cursor|курсор|wordpress|вордпрес|вордпресс|"
    r"контент[-\s]?завод|контент[-\s]?конвейер|"
    r"claude|gemini|deepseek|copilot|perplexity|\bllama\b|\bgrok\b|"
    r"(?<![а-я])ии[-\s]?ассистент|(?<![а-я])ии[-\s]?агент|чат[-\s]?бот|"
    r"файнтюн|база знаний|"
    r"\bсайт\b|лендинг|интернет-магазин|трафик|"
    r"\b1с\b|\b1c\b|\bcrm\b|битрикс|bitrix|мойсклад|дзен|"
    r"карточки товар|карточки для|создан\w+\s+карточ)",
    re.IGNORECASE,
)

# Роли, которые не становятся заказом из-за слова «сайт» или «бот».
HARD_REJECT = re.compile(
    r"(весь список актуальных вакансий|яндекс\.?\s?еда|кладовщик|пеший курьер|"
    r"водитель-курьер|"
    r"нейросети делают|опыт не нужен|первые деньги можно получить|"
    r"рилсмейкер|монтаж[её]р|ретуш[её]р|"
    r"креатор|копирайтер|контекстолог|закупк\w+\s+реклам|"
    r"kling|runway|midjourney|нейрофото|ии-видео|ai-видео|генерац\w+\s+видео|"
    r"менеджер по продажам|таргетолог|тильдолог|маркетолог|"
    r"дизайнер\w*|дизайн\w{0,6}\s+карточки|инфографик|пару слайдов|(?<![а-я])слайд(?![а-я])|"
    r"оформлени\w+ карточки|визуал\w* карточки|"
    r"недвижимост|агент\w{0,6}\s+по\s+продаж|ри[эе]лтор|менеджерпопродажам|"
    r"(?<![а-я])рилс(?![а-я])|reels|сценарист|"
    r"обработк\w+\s+входящих\s+заявок|"
    r"художник|баннер|режисс[её]р|арт-?директор|"
    r"content creator|видеоконтент|сторисмейкер|директолог|"
    r"писател\w*|верстальщик|помаркетинг|продюсер запусков|"
    r"проверяющ\w+\s+ответов|проверять ответы нейросет|"
    r"как найти таких|"
    r"ищ(?:ет|ут)\s+(?:[0-9a-zа-я#+.-]+\s+){0,5}"
    r"(?:developer|engineer|creator|художник|режисс))",
    re.IGNORECASE,
)

SLAG = re.compile(
    r"(весь список актуальных вакансий|яндекс\.?\s?еда|кладовщик|пеший курьер|"
    r"водитель-курьер|"
    r"нейросети делают|опыт не нужен|первые деньги можно получить|"
    r"рилсмейкер|монтаж[её]р|ретуш[её]р|smm-менеджер|смм-менеджер|"
    r"креатор|копирайтер|контекстолог|закупк\w+\s+реклам|"
    r"kling|runway|midjourney|нейрофото|ии-видео|ai-видео|генерац\w+\s+видео|"
    r"менеджер по продажам|таргетолог|тильдолог|маркетолог|"
    r"занимаюсь разработкой|чем могу быть полезн|"
    r"делаем сайты и веб-приложения|наше агентское портфолио|"
    r"дизайнер\w*|дизайн\w{0,6}\s+карточки|инфографик|пару слайдов|(?<![а-я])слайд(?![а-я])|"
    r"оформлени\w+ карточки|визуал\w* карточки|"
    r"недвижимост|агент\w{0,6}\s+по\s+продаж|ри[эе]лтор|менеджерпопродажам|"
    r"(?<![а-я])рилс(?![а-я])|reels|сценарист|"
    r"обработк\w+\s+входящих\s+заявок|полностью удал|"
    r"перелив\w*\s+трафика|залив\w*\s+трафика|привлечени\w+\s+трафика|"
    r"художник|баннер|режисс[её]р|арт-?директор|"
    r"content creator|видеоконтент|сторисмейкер|директолог|"
    r"(?<!не )парсер)",
    re.IGNORECASE,
)

SPAM = re.compile(
    r"(подписывайся|переходи по ссылке|успей записаться|только сегодня|"
    r"бесплатный доступ|взаимопиар|"
    r"автоматически публикует объявления|"
    r"рассылк\w+ объявлен|"
    r"₽\s*/\s*мес|подписк\w+.{0,24}в месяц|далее\s*[—\-]\s*\d|"
    r"заработал\s*\+|заработал\s+\d|пошаговой инструкц|уволиться из найма|"
    r"ищете работу|запустили telegram|вакансии без опыта|подработк|"
    r"удал[её]нн\w+\s+професси\w+\s+за|ваш старт в профессии|"
    r"навыки, за которые платят|"
    r"нужны вложения|возможность заработать|"
    r"продвигаем любые офферы|реклама вашего оффера|"
    r"пробив\w*|сканы паспорт|"
    r"ищу\s+(?:проектную|постоянную|удал[её]нн\w+)(?:\s+\w+){0,4}\s+работу)",
    re.IGNORECASE,
)

SYSTEM_BUILD = re.compile(
    r"(конвейер|автоматизац|\bn8n\b|\bmake\b|make\.com|"
    r"cursor|курсор|wordpress|вордпрес|контент[-\s]?завод|"
    r"генерац\w+\s+(описан|карточек)|чат[-\s]?бот|(?<![а-я])ии[-\s]?агент|"
    r"файнтюн|база знаний|\b1с\b|\b1c\b|битрикс|bitrix|мойсклад|"
    r"\bсайт\b|лендинг|"
    r"карточк\w+\s+товар|созда(ть|ни[ея]|вать)\s+карточ|"
    r"дзен|автопостинг)",
    re.IGNORECASE,
)

HIRING = re.compile(
    r"((?<!не )в штат|оклад|\bзп\b|з\s*/\s*п|зарплат|"
    r"полная занятость|релокац|"
    r"в команду|ищем сотрудника|"
    r"₽\s*(?:в|/)\s*месяц|руб\w*\s*(?:в|/)\s*месяц|"
    r"трудоустройство|оформление по тк|"
    r"вакансия компании|уровень месячного|месячного дохода|"
    r"офис в\s)",
    re.IGNORECASE,
)

HIRING_HARD = re.compile(
    r"((?<!не )в штат|оклад|\bзп\b|з\s*/\s*п|зарплат|"
    r"полная занятость|релокац|"
    r"₽\s*(?:в|/)\s*месяц|руб\w*\s*(?:в|/)\s*месяц|"
    r"трудоустройство|оформление по тк|"
    r"вакансия компании|уровень месячного|месячного дохода)",
    re.IGNORECASE,
)

PROJECT_HIRE = re.compile(
    r"(подряд|не в штат|аутсорс|\bгпх\b|проектн\w*|на проект)",
    re.IGNORECASE,
)

ONE_OFF = re.compile(
    r"(подвинуть кнопку|поправить кнопку|"
    r"волшебн\w+\s+кнопк|магическ\w+\s+кнопк|"
    r"проверить поиск|"
    r"мелк\w+\s+правк|разов\w+\s+правк|копеечн|"
    r"поправить текст|исправить опечатк|"
    r"подправить (сайт|текст|кнопк)|"
    r"внести правк\w+ на сайт|правк\w+\s+сайта|"
    r"решить проблему с сайтом)",
    re.IGNORECASE,
)

TENDER = re.compile(
    r"(тендер|44-\s*фз|223-\s*фз|zakupki\.gov|"
    r"конкурсн\w+\s+документац|закупочн\w+\s+(документац|процедур)|"
    r"госзакуп|электронн\w+\s+аукцион|запрос\s+котировок|"
    r"извещени\w+\s+о\s+закупк|участ\w+\s+в\s+закупк)",
    re.IGNORECASE,
)

SMM_TOPIC = re.compile(
    r"(\bsmm\b|смм|smm-менеджер|смм-менеджер|"
    r"контент[-\s]?план|копирайт|"
    r"рилсмейкер|(?<![а-я])рилс(?![а-я])|\breels\b|"
    r"съ[её]мк|посев\w*|ведение соцсет|продвижение в соцсет|"
    r"выкладк\w+\s+контент|ассистент\w*\s+по\s+оформлен|"
    r"написани\w+\s+отзыв|качественн\w+\s+стать|экспертн\w+\s+пост)",
    re.IGNORECASE,
)

SMM_HANDS = re.compile(
    r"(копирайт|съ[её]мк|рилсмейкер|(?<![а-я])рилс(?![а-я])|\breels\b|"
    r"сторис|\bstories\b|контент[-\s]?план|"
    r"вести (инстаграм|соцсет|аккаунт)|посев\w*|"
    r"качественн\w+\s+стать|экспертн\w+\s+пост)",
    re.IGNORECASE,
)

SMM_SPECIFIC = re.compile(
    r"(дзен|автопостинг|авто-постинг|"
    r"контент[-\s]?конвейер|контент[-\s]?завод|"
    r"трафик[-\s]?систем|"
    r"систем\w+\s+(автопостинг|постинг|публикац|статей|контент)|"
    r"\bn8n\b|make\.com)",
    re.IGNORECASE,
)

OFFER_HEAD = re.compile(
    r"^\s*(сделаю|выполню|разработаю|настрою|создам)\b",
    re.IGNORECASE,
)

_MONEY = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+|\d{3,7})\s*(?:₽|руб(?:\.|лей|ля|ль)?|(?:р(?![а-яa-z])))",
    re.IGNORECASE,
)
_MONEY_WORD = re.compile(
    r"(?:бюджет|оплата|гонорар|стоимость|чек)\D{0,20}(\d{1,3}(?: \d{3})+|\d{4,7})\b",
    re.IGNORECASE,
)
_MONEY_K = re.compile(
    r"(?<!\d)(\d{2,3})\s*(?:к|тыс\.?)\b(?!\s*(?:подпис|человек|товар|карточ|sku|просмотр))",
    re.IGNORECASE,
)

DEFAULT_PRICE_FLOOR = 5000
DEFAULT_SMM_BUDGET = 50_000


def client_opens(text: str, window: int = 250) -> bool:
    """Явная фраза заказчика в начале поста, не «ищут» и не «ищете».

    «Нужен сайт? Помогу создать» — это визитка, не заказ.
    """
    head = _plain(text)[:window]
    if not CLIENT_OPEN.search(head):
        return False
    if re.search(r"помогу|наши работы|работаю на бизнес", head) and not re.search(
        r"(?<![а-я])(ищу|ищем)\s+(разработчик|подрядчик|исполнител|специалист)",
        head,
    ):
        return False
    return True


def rubles(text: str) -> list[int]:
    plain = _plain(text)
    found: list[int] = []
    for match in _MONEY.finditer(plain):
        digits = re.sub(r"\s+", "", match.group(1))
        if digits.isdigit():
            found.append(int(digits))
    for match in _MONEY_WORD.finditer(plain):
        digits = re.sub(r"\s+", "", match.group(1))
        if digits.isdigit():
            found.append(int(digits))
    for match in _MONEY_K.finditer(plain):
        found.append(int(match.group(1)) * 1000)
    return found


def price_floor() -> int:
    raw = load_profile().get("min_budget_rub")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICE_FLOOR


def smm_min_budget() -> int:
    raw = (load_profile().get("smm") or {}).get("min_budget_rub")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SMM_BUDGET


def is_hiring(text: str) -> bool:
    plain = _plain(text)
    if not HIRING.search(plain):
        return False
    if HIRING_HARD.search(plain):
        return True
    if re.search(r"(в команду|ищем сотрудника)", plain) and not PROJECT_HIRE.search(plain):
        return True
    return False


def is_offer_card(text: str) -> bool:
    """Карточка исполнителя на бирже: «сделаю / от N ₽ за», а не заказ с бюджетом."""
    plain = _plain(text).strip()
    if not plain:
        return False
    if OFFER_HEAD.search(plain):
        return True
    seller_price = re.search(r"от\s+\d[\d\s]*\s*(?:₽|руб)\s+за\b", plain)
    if seller_price and not re.search(r"(?<![а-я])бюджет(?![а-я])", plain) and not client_opens(plain):
        return True
    if re.search(r"(принимаю заказы|беру заказы|мои услуги)", plain) and re.search(
        r"(сделаю|выполню|от\s+\d)", plain
    ):
        if not client_opens(plain):
            return True
    return False


def smm_block(text: str) -> str | None:
    """SMM берём широко (решение Павла 2026-10-11): отсекаем только явно копеечный бюджет."""
    plain = _plain(text)
    if not SMM_TOPIC.search(plain):
        return None
    amounts = rubles(plain)
    if amounts and max(amounts) < smm_min_budget():
        return f"smm с бюджетом ниже {smm_min_budget():,} ₽".replace(",", " ")
    return None
    if re.search(r"не\s+(нужен |нужна |нужно |брать )?smm|не\s+смм", plain):
        return None
    specific = bool(SMM_SPECIFIC.search(plain))
    hands = bool(SMM_HANDS.search(plain))
    if hands and not specific:
        return "smm только про копирайт, съёмку, рилс или контент-план"
    if not specific:
        return "smm без конкретики: канал, задача и результат не названы"
    amounts = rubles(plain)
    if not amounts or max(amounts) < smm_min_budget():
        return "smm дешевле 50 000 ₽ или без бюджета"
    return None


def hashtag_wall(text: str) -> bool:
    tags = re.findall(r"#[0-9a-zа-я_]+", _plain(text))
    return len(tags) >= 4 and not client_opens(text)


def normalized_text(text: str) -> str:
    """Один и тот же текст из разных чатов сходится к одной строке."""
    lowered = (text or "").lower().replace("ё", "е")
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[@#][a-zа-я0-9_]+", " ", lowered)
    lowered = re.sub(r"[^a-zа-я0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(normalized_text(text).encode("utf-8")).hexdigest()[:32]


def prefix_fingerprint(text: str) -> str:
    """Начало объявления. Перепост с другим хвостом считается тем же."""
    norm = normalized_text(text)
    chunk = norm[:220]
    if len(chunk) < 80:
        return text_fingerprint(text)
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:32]


def is_spam(text: str) -> bool:
    text = _plain(text)
    if SPAM.search(text) or PRODUCT_AD.search(text):
        return True
    if len(re.findall(r"https?://", text)) >= 4 and not client_opens(text):
        return True
    lines = [re.sub(r"\s+", " ", line).strip().lower() for line in text.splitlines()]
    lines = [line for line in lines if len(line) > 50]
    return len(lines) >= 3 and len(set(lines)) < len(lines)


def not_my_job(text: str) -> str | None:
    """Реклама, отзыв, визитка, найм, дешёвый SMM, разовая правка или чужая вакансия."""
    text = _plain(text)
    if is_spam(text):
        return "реклама или спам"
    if REVIEW_CASE.search(text) and not client_opens(text):
        return "отзыв или кейс исполнителя"
    if (SELF_PROMO_STRONG.search(text) or BUSINESS_CARD.search(text) or hashtag_wall(text)) and not client_opens(text):
        return "исполнитель рекламирует себя"
    if ONE_OFF.search(text):
        return "разовая правка или волшебная кнопка"
    if TENDER.search(text):
        return "тендер"
    if is_hiring(text):
        return "штатная вакансия"
    if is_offer_card(text):
        return "карточка услуги, не заказ"
    smm = smm_block(text)
    if smm:
        return smm
    if MARKETPLACE_TOPIC.search(text) and not PRODUCT_CARDS.search(text):
        return "по маркетплейсам беру только создание карточек товара"
    if MARKETPLACE_MANAGER.search(text) and not PRODUCT_CARDS.search(text):
        return "ищут менеджера маркетплейса, не карточки"
    if HARD_REJECT.search(text):
        return "чужая вакансия или реклама"
    # «Сайт» или «бот» сами по себе больше не выключают отсев мусора.
    if SLAG.search(text) and not (SYSTEM_BUILD.search(text) and CLIENT_INTENT.search(text)):
        return "чужая вакансия или реклама"
    if HELPER_JOB.search(text) and not REAL_SERVICE.search(text):
        return "ищут живого помощника, не услугу"
    if SELF_PROMO.search(text) and not client_opens(text):
        return "исполнитель рекламирует себя"
    return None


def too_cheap(text: str, floor: int | None = None) -> bool:
    """Явная цена, если самая большая сумма ниже порога. «От 80 000» не режем."""
    amounts = rubles(text)
    if not amounts:
        return False
    limit = price_floor() if floor is None else floor
    return max(amounts) < limit


def intake_hits(text: str, sources: dict, *, assume_request: bool = False) -> list[str] | None:
    """Проходит заказ с ключом услуги. В чатах нужно слово запроса. На бирже карточка проверяется отдельно."""
    if hits_stopword(text, sources.get("stopwords") or []):
        return None
    if learned_block(text):
        return None
    if too_cheap(text):
        return None
    if not_my_job(text):
        return None
    if assume_request and is_offer_card(text):
        return None
    if not assume_request and not SEEK_WORD.search(text):
        return None
    hits = match_keywords(text, sources.get("keywords") or [])
    if not hits:
        return None
    return hits
