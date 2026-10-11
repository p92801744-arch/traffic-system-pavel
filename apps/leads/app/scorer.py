from __future__ import annotations

import logging
import re

from app.filters import load_profile, not_my_job
from app.lessons import learned_block

log = logging.getLogger("leads.scorer")

COMMERCIAL = re.compile(
    r"(ищ(у|ем)|нуж(ен|на|но|ны)|заказ|ваканси|бюджет|прайс|сделать|разработ|"
    r"внедри|настроит|требуе|ставк|гпх|full[- ]?time|фулл.?тайм|"
    r"отклик|резюме|\bcv\b|проект:|задача:|оплат|гонорар|стоимость|"
    r"разобра|разбор)",
    re.IGNORECASE,
)

CHAT_NOISE = re.compile(
    r"(хах+|ахах+|лол|пиздец|блин заставить|на совещани|"
    r"жене настроить|в автобусе|копипаст тупой)",
    re.IGNORECASE,
)

FREELANCE_AD = re.compile(
    r"(freelance assistant|хватит бесконечно скрол|"
    r"мониторит (популярные )?фриланс|"
    r"находит заказы за тебя|"
    r"бот сам сгенерирует отклик|"
    r"доверь это боту|чистую ленту без спама)",
    re.IGNORECASE,
)

SELLER = re.compile(
    r"(#помогу|#услуги|#приглашаю|обращайтесь|пишите в лс|пиши(те)? мне|"
    r"портфолио в шапке|возьму на себя|я (создаю|разрабатываю|занимаюсь|делаю)|"
    r"меня зовут .{0,40}я |мои услуг|открыта к сотрудничеству|"
    r"пришлём предложение|напишите, что нужно)",
    re.IGNORECASE,
)

CLIENT_ASK = re.compile(
    r"(ищ(у|ем)|нуж(ен|на|но|ны)|требует(ся)|кто (может|сделает|возьмёт)|"
    r"заказ[:\s]|бюджет|оплачу|готова? платить|разобра|разбор)",
    re.IGNORECASE,
)

STAFF = re.compile(
    r"(в штат|штатн|полная занятость|full[- ]?time|фулл.?тайм|"
    r"ищем сотрудника|ищем в команду|оформление в штат|резюме|\bcv\b|"
    r"соискател|вакансия (разработ|программист|менеджер))",
    re.IGNORECASE,
)

AI_CORE = re.compile(
    r"(нейросет|chatgpt|gpt-?4|llm\b|чат[- ]?бот|телеграм.?бот|telegram.?бот|"
    r"\bn8n\b|make\.com|интегр|автоматизац|\bcrm\b|amocrm|"
    r"битрикс|агентск|ai[- ]агент|ии[- ]агент|воронк|"
    r"\bии\b|искусственн\w+ интеллект|карточки товар|контент[- ]конвейер|"
    r"каталог товар|ассистент|база знаний|\brag\b|1с|1c|дзен)",
    re.IGNORECASE,
)

AGENTS = re.compile(
    r"(агентск|ии[- ]агент|ai[- ]агент|вызов инструмент|эскалац|"
    r"созда(ть|вать) сделк|действие в (crm|учёт))",
    re.IGNORECASE,
)

BOTS = re.compile(
    r"(чат[- ]?бот|телеграм[- ]?бот|telegram[- ]?бот|тг[- ]?бот|"
    r"бот для|whatsapp[- ]?бот|вотсап[- ]?бот|вк[- ]?бот|"
    r"ии[- ]?ассистент|ai[- ]?ассистент|ии[- ]помощник|gpt[- ]?помощник)",
    re.IGNORECASE,
)

N8N = re.compile(
    r"(\bn8n\b|make\.com|\bmake\b|связк\w+ (сервис|приложен)|вебхук|webhook|"
    r"заявк\w+ в crm|из telegram в|из вотсап|whatsapp.{0,20}crm|"
    r"интеграц|\bapi\b|подключ\w+ (сервис|приложен|crm|1с))",
    re.IGNORECASE,
)

ONEC = re.compile(r"(\b1с\b|\b1c\b|обмен с 1)", re.IGNORECASE)

KB = re.compile(
    r"(база знаний|\brag\b|регламент\w*|со ссылкой на источник|"
    r"ответы по документ|по внутренним документ|по переписке)",
    re.IGNORECASE,
)

DOCS = re.compile(
    r"(разбор (договор|счет|счёт|акт|документ)|разобрать (договор|счет|счёт|акт)|"
    r"счета и акт|акт(ы|ов)? выполн|распозна(ть|вание) документ|"
    r"извлечь поля|в поля crm|в поля 1с|скан\w+ (договор|счет|счёт|акт))",
    re.IGNORECASE,
)

CATALOG = re.compile(
    r"(карточки товар|товарн\w+ карточ|описани\w+ товар|контент[- ]конвейер|"
    r"каталог товар|ассортимент|номенклатур|seo.?описан|прайс поставщик|"
    r"созда(ть|ни[ея]|вать)\s+карточ|"
    r"карточки (для |на )?(wb|вб|ozon|озон|wildberries|маркетплейс))",
    re.IGNORECASE,
)

DZEN = re.compile(
    r"(яндекс\.?\s?дзен|\bдзен\b|дзен.?канал|статьи на дзен|"
    r"статейн\w+ сервис|vc\.ru|pulse\.mail|продвижение статей)",
    re.IGNORECASE,
)

SHOP = re.compile(
    r"(интернет[- ]?магазин|владельц\w+ магазин|магазин аккумулятор|"
    r"свой канал продаж|e-?commerce)",
    re.IGNORECASE,
)

WEB = re.compile(
    r"(разработ\w+ сайт|нуж(ен|на) сайт|сделать сайт|сайт под ключ|"
    r"лендинг|корпоративн\w+ сайт|интернет[- ]?магазин|"
    r"wordpress|вордпресс|тильд[аы]|веб[- ]?разработ|"
    r"визитк\w+ сайт|многостраничн)",
    re.IGNORECASE,
)

FINETUNE = re.compile(
    r"(файнтюн|fine[- ]?tun|дообуч\w+ модел|обуч\w+ модел|"
    r"обучить модел|дообучить|lora\b|лора\b|"
    r"на своих данных|на наших данных|на данных компани)",
    re.IGNORECASE,
)

CORE_PATTERNS = (
    AGENTS,
    BOTS,
    N8N,
    ONEC,
    KB,
    DOCS,
    CATALOG,
    DZEN,
    SHOP,
    WEB,
    FINETUNE,
)

OFF_SITE = re.compile(
    r"(голосовой робот|речевая аналитика|голос(овой)? помощник|"
    r"нужен парсер чуж|скрапер|парсинг сайтов|замена отдела)",
    re.IGNORECASE,
)

COURSE = re.compile(
    r"(научу делать|набор на курс|марафон|наставничеств|обуч\w+ ии|"
    r"курс по (ии|нейро|chatgpt))",
    re.IGNORECASE,
)

CRYPTO = re.compile(
    r"(крипт|usdt|казино|пушкинск\w+ карт)",
    re.IGNORECASE,
)


def _reject(why: str) -> dict:
    return {
        "fit": 1,
        "is_paid_order": False,
        "high_ticket": False,
        "why": why,
        "suggested_offer": "не подходит",
    }


def _score(fit: int, why: str, *, paid: bool = False, high: bool = False, offer: str = "не подходит") -> dict:
    return {
        "fit": max(1, min(5, fit)),
        "is_paid_order": paid,
        "high_ticket": high,
        "why": why,
        "suggested_offer": offer,
    }


def _reject_from_lessons(text: str) -> dict | None:
    blocked = learned_block(text)
    if not blocked:
        return None
    return _reject(f"Не отдавал бы: {blocked}")


def first_pass(text: str) -> dict | None:
    """Явный мусор. Живые заявки идут в полную оценку."""
    if CHAT_NOISE.search(text) and not COMMERCIAL.search(text):
        return _reject("Не отдавал бы: это болтовня в чате, а не заказ.")
    if FREELANCE_AD.search(text):
        return _reject("Не отдавал бы: это реклама чужого бота, который ищет заказы за фрилансера.")
    off = not_my_job(text)
    if off == "ищут живого помощника, не услугу":
        return _reject("Не отдавал бы: ищут живого помощника на смену или в кабинет. Про ИИ и автоматизацию тут ничего нет.")
    if off == "исполнитель рекламирует себя":
        return _reject("Не отдавал бы: это исполнитель рекламирует себя, а не заказчик с задачей.")
    if off == "реклама или спам":
        return _reject("Не отдавал бы: это реклама или спам, а не заказ.")
    if off == "реклама чужого сервиса":
        return _reject("Не отдавал бы: это реклама чужого сервиса, а не заказчик, которому нужно что-то сделать.")
    if off == "чужая вакансия или реклама":
        return _reject("Не отдавал бы: это чужая вакансия или разовая работа дизайнера, не система.")
    learned = _reject_from_lessons(text)
    if learned:
        return learned
    return None


def _hits_core(text: str) -> bool:
    return any(rx.search(text) for rx in CORE_PATTERNS)


def _topic(text: str) -> tuple[str, str, str]:
    """Именительный, винительный, оффер."""
    if FINETUNE.search(text):
        return (
            "дообучение модели",
            "дообучение модели",
            "дообучение модели на ваших примерах, чтобы ответы были про ваш продукт",
        )
    if KB.search(text):
        return (
            "база знаний",
            "базу знаний",
            "база знаний: ответ по вашим документам со ссылкой на источник",
        )
    if DOCS.search(text):
        return (
            "разбор документов",
            "разбор документов",
            "разбор договора, счёта или акта в поля CRM или 1С",
        )
    if ONEC.search(text):
        return (
            "связка с 1С",
            "связку с 1С",
            "связка ваших приложений, включая 1С, без ручного переноса",
        )
    if CATALOG.search(text) and re.search(
        r"wildberries|\bozon\b|маркетплейс|\bвб\b|\bwb\b|карточ",
        text,
        re.IGNORECASE,
    ):
        return (
            "карточки для маркетплейса",
            "карточки для Wildberries или Ozon",
            "карточки для Wildberries или Ozon, как в кейсе каталога",
        )
    if DZEN.search(text):
        return (
            "продвижение в Дзене",
            "статьи и продвижение в Дзене",
            "статьи и продвижение в Дзене или похожем сервисе",
        )
    if WEB.search(text) and not CATALOG.search(text):
        return (
            "разработка сайта",
            "сайт",
            "сайт или магазин под задачу: структура, запуск, при необходимости ИИ-модуль",
        )
    if AGENTS.search(text):
        return (
            "агентская система",
            "агентскую систему",
            "пилот агентской системы: заявка в мессенджере → действие в CRM, спорное человеку",
        )
    if BOTS.search(text):
        return (
            "бот или ассистент",
            "бота или ИИ-ассистента",
            "бот или ИИ-ассистент под задачу: ответы клиентам и передача заявки в учёт",
        )
    if CATALOG.search(text) or (SHOP.search(text) and re.search(r"карточк|описан|каталог", text, re.IGNORECASE)):
        return (
            "контент-конвейер каталога",
            "контент-конвейер каталога",
            "контент-конвейер карточек, как в кейсе аккумуляторов",
        )
    if N8N.search(text) or re.search(r"автоматизац", text, re.IGNORECASE):
        return (
            "связка приложений",
            "связку приложений",
            "сценарий на n8n или Make: мессенджер, CRM и приложения без ручного переноса",
        )
    if SHOP.search(text):
        return (
            "магазин и каталог",
            "магазин и каталог",
            "живой каталог и конвейер карточек для своего канала продаж",
        )
    if re.search(r"нейросет|\bии\b|chatgpt", text, re.IGNORECASE):
        return (
            "задача на ИИ",
            "задачу на ИИ",
            "разбор в Telegram: бот, база знаний, 1С, каталог или Дзен — что ближе к боли",
        )
    return (
        "автоматизация",
        "работу по автоматизации",
        "разбор процесса и узкий контур под ваши сервисы",
    )


def judge(text: str, channel: str | None = None) -> dict:
    """Всегда ставит балл и пишет мнение, почему заявку отдать или нет."""
    quick = first_pass(text)
    if quick:
        return quick

    if CRYPTO.search(text):
        return _reject("Не отдавал бы: крипта, казино или серая схема — это не твой заказ.")
    if COURSE.search(text):
        return _reject("Не отдавал бы: здесь продают курс или «научу», а не просят сделать систему.")
    if OFF_SITE.search(text) and not _hits_core(text):
        return _score(
            2,
            "Почти не отдавал: голосовые роботы и парсинг чужих сайтов ты не берёшь.",
        )

    seller = bool(SELLER.search(text))
    client = bool(CLIENT_ASK.search(text))
    staff = bool(STAFF.search(text))
    ai = bool(AI_CORE.search(text) or _hits_core(text))
    shop = bool(SHOP.search(text) or CATALOG.search(text) or WEB.search(text) or DZEN.search(text))
    core = _hits_core(text)
    topic_nom, topic_acc, site_offer = _topic(text)

    paid_brief = bool(re.search(r"бюджет|оплачу|готова? платить|кто (может|сделает)", text, re.IGNORECASE))

    if staff and not paid_brief:
        return _score(
            2,
            "Не отдавал бы: это штатная вакансия, ищут сотрудника в команду, а не подрядчика на проект.",
            offer="не подходит",
        )

    if seller and not paid_brief:
        if ai:
            return _score(
                1,
                "Не отдавал бы: это самореклама другого исполнителя (боты/автоматизация), заказчика в посте нет.",
            )
        return _score(
            1,
            "Не отдавал бы: человек продаёт свои услуги. Тебе нужен заказчик, который платит, а не конкурент.",
        )

    if client and ai:
        high = shop or bool(re.search(r"внедри|под ключ|сопровожден", text, re.IGNORECASE))
        fit = 5 if core and high else (4 if core else 3)
        extra = " Это твоя услуга." if core else (
            " Проверь, бот это, база знаний, 1С, карточки, сайт или Дзен."
        )
        return _score(
            fit,
            f"Отдал, потому что это похоже на платный заказ: просят сделать {topic_acc}.{extra}",
            paid=True,
            high=high or core,
            offer=site_offer,
        )

    if client and not ai:
        if shop:
            return _score(
                4,
                "Отдал: заказчик с магазином, сайтом или каталогом. "
                "Это твоя разработка сайта или конвейер карточек, как в кейсе аккумуляторов.",
                paid=True,
                high=True,
                offer=site_offer,
            )
        return _score(
            2,
            f"Почти не отдавал: заказчик вроде есть, но это не бот, не база знаний, не 1С, не каталог, не сайт и не Дзен. "
            f"Тема ближе к «{topic_nom}».",
            paid=True,
        )

    if ai and COMMERCIAL.search(text):
        return _score(
            3 if core else 2,
            f"Отдал с оговоркой: в посте есть {topic_nom}, но не до конца ясно, это заказчик или исполнитель. "
            "Если платят и процесс повторяется — это твоё.",
            paid=True,
            offer=site_offer,
        )

    if not COMMERCIAL.search(text):
        return _reject("Не отдавал бы: обычное сообщение, заказа не видно.")

    return _score(
        2,
        "Почти не отдавал: по ключевым словам похоже на работу, но не вижу ясного платного заказа на ИИ.",
    )


def score_with_profile(text: str, channel: str | None, judgement: dict) -> dict:
    """Нормализует оценку, если её когда-нибудь допишет облачный агент."""
    profile = load_profile()
    fit = max(1, min(5, int(judgement.get("fit") or 0)))
    why = str(judgement.get("why") or "").strip()
    if not why:
        who = (profile.get("who") or "").split("\n")[0]
        why = f"Оценка по профилю: {who}"
    return {
        "fit": fit,
        "is_paid_order": bool(judgement.get("is_paid_order")),
        "high_ticket": bool(judgement.get("high_ticket")),
        "why": why,
        "suggested_offer": str(judgement.get("suggested_offer") or "не подходит"),
    }


async def score_lead(text: str, channel: str | None = None) -> dict:
    """Сначала жёсткий локальный отсев, затем Jev (если есть ключ), иначе правила."""
    quick = first_pass(text)
    if quick:
        return quick

    from app import jev

    if jev.enabled():
        try:
            result = await jev.score_lead_text(text, channel)
            # Подмешиваем оффер из локальной темы, если Jev не уточнил карточки.
            if result.get("suggested_offer") == (
                "разбор в Telegram: бот, база знаний, 1С, каталог или Дзен — что ближе к боли"
            ):
                _nom, _acc, site_offer = _topic(text)
                if site_offer and site_offer != "не подходит":
                    result = {**result, "suggested_offer": site_offer}
            return result
        except Exception:
            log.exception("Jev не ответил — локальная оценка")

    return judge(text, channel)
