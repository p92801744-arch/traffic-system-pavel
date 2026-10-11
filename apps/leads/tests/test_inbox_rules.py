"""Приёмка фильтра и голоса отклика по правилам Павла."""

from __future__ import annotations

import json
from pathlib import Path

from app.filters import (
    client_opens,
    hits_stopword,
    intake_hits,
    is_hiring,
    is_offer_card,
    load_sources,
    match_keywords,
    not_my_job,
    smm_block,
    too_cheap,
)
from app.lessons import learned_block, reason_class
from app.reply import _prompt, reply_problems
from app.scorer import judge
from app.settings import ROOT

LESSONS = ROOT / "data" / "reject-lessons.json"

GOOD_BOT = (
    "Ищу разработчика чат-бота для интернет-магазина. "
    "Нужно связать Telegram и CRM на n8n, чтобы заявки не терялись. "
    "Бюджет 80 000 ₽, срок месяц."
)
GOOD_BITRIX = (
    "Ищу подрядчика на связку Битрикс24 и 1С: автоматизация HR-процессов, "
    "перенос заявок из CRM в учёт. Бюджет 120 000 руб."
)
GOOD_CATALOG = (
    "Ищем исполнителя на контент-конвейер карточек для интернет-магазина. "
    "Каталог, описания товаров и выгрузка на сайт. Бюджет 90 000 ₽."
)
GOOD_SMM = (
    "Ищу специалиста по SMM. Бюджет 70 000 ₽. "
    "Нужна система автопостинга статей в Дзен: темы, проверка и публикация. "
    "Канал Дзен, результат — регулярные статьи без ручной выкладки."
)
GOOD_INTRO = (
    "Меня зовут Анна, я владелец магазина. "
    "Ищу разработчика чат-бота на n8n, бюджет 60 000 ₽."
)

AUDIT_IDS = (4391, 4313, 4404, 4399, 4438, 4303, 4400, 4394, 4405, 4398, 4403)


def _lessons() -> list[dict]:
    data = json.loads(LESSONS.read_text(encoding="utf-8"))
    return [row for row in data["lessons"] if isinstance(row, dict)]


def _about(lead_id: int) -> str:
    for row in _lessons():
        if row.get("lead_id") == lead_id:
            return row.get("about") or ""
    raise AssertionError(f"в уроках нет заявки {lead_id}")


def _sources() -> dict:
    return load_sources()


def test_stopword_does_not_hit_inside_script():
    text = "Ищу разработчика, нужен скрипт для чат-бота на сайте, бюджет 60 000 руб"
    assert hits_stopword(text, _sources()["stopwords"]) is None
    assert intake_hits(text, _sources())


def test_stopword_still_hits_crypto():
    text = "Ищу разработчика бота для крипты, бюджет 60 000 руб"
    assert hits_stopword(text, _sources()["stopwords"])


def test_vacancy_agent_is_not_ai_agent():
    hits = match_keywords("вакансии агент ищу сайт", _sources()["keywords"])
    assert "ии агент" not in [item.lower() for item in hits]
    assert any("сайт" in item.lower() for item in hits)


def test_self_promo_is_not_saved_by_ishchut():
    text = (
        "Меня зовут Дмитрий! Я — разработчик чат-ботов на n8n. "
        "Клиенты, которые ищут ваши услуги, пишите в лс. Автоматизация и сайт."
    )
    assert not client_opens(text)
    assert not_my_job(text)
    assert intake_hits(text, _sources(), assume_request=True) is None


def test_review_rejected():
    text = (
        "Спасибо за работу! Рекомендую: сделали для клиента бота на n8n. "
        "Наш кейс: магазин, результат: +30% заявок."
    )
    assert not_my_job(text) == "отзыв или кейс исполнителя"
    assert intake_hits(text, _sources(), assume_request=True) is None


def test_hiring_rejected():
    text = (
        "Ищем в штат разработчика чат-ботов и n8n, оклад 150 000 ₽ в месяц, "
        "офис в Москве, полная занятость."
    )
    assert is_hiring(text)
    assert not_my_job(text) == "штатная вакансия"
    assert intake_hits(text, _sources(), assume_request=True) is None
    assert judge(text)["fit"] <= 2


def test_project_contractor_is_not_staff():
    text = (
        "Ищу подрядчика в команду на проект автоматизации n8n, не в штат. "
        "Бюджет 100 000 ₽, нужен разработчик сценария заявок."
    )
    assert not is_hiring(text)
    assert intake_hits(text, _sources())


def test_smm_low_budget_and_vague_rejected():
    low = (
        "Ищу SMM. Бюджет 20 000 ₽. Нужен автопостинг статей в Дзен и контент-конвейер."
    )
    vague = (
        "Ищу SMM-специалиста, бюджет 80 000 ₽. "
        "Нужен контент-план, рилс и копирайтинг для инстаграма."
    )
    assert "50 000" in (smm_block(low) or "")
    assert smm_block(vague)
    assert intake_hits(low, _sources(), assume_request=True) is None
    assert intake_hits(vague, _sources(), assume_request=True) is None


def test_smm_with_budget_and_specifics_passes():
    assert smm_block(GOOD_SMM) is None
    assert intake_hits(GOOD_SMM, _sources())
    assert judge(GOOD_SMM)["fit"] >= 3


def test_offer_card_rejected_and_client_order_kept():
    offer = "Сделаю чат-бота под ключ на n8n. От 15 000 ₽ за бота. Пишите в лс."
    order = "Ищу разработчика чат-бота на n8n, бюджет 80 000 ₽, нужен подрядчик."
    assert is_offer_card(offer)
    assert not is_offer_card(order)
    assert intake_hits(offer, _sources(), assume_request=True) is None
    assert intake_hits(order, _sources(), assume_request=True)


def test_one_off_magic_button_and_tender():
    one_off = "Нужно проверить поиск на сайте и сообщить, если есть ошибки. Бюджет 8 000 ₽."
    button = "Нужна волшебная кнопка, чтобы нейросеть сама вела продажи и сайт."
    tender = "Тендер по 44-ФЗ на разработку сайта и чат-бота, конкурсная документация во вложении."
    assert not_my_job(one_off) == "разовая правка или волшебная кнопка"
    assert not_my_job(button) == "разовая правка или волшебная кнопка"
    assert not_my_job(tender) == "тендер"
    for text in (one_off, button, tender):
        assert intake_hits(text, _sources(), assume_request=True) is None


def test_from_price_below_floor_is_cheap():
    assert too_cheap("Нужно сделать сайт, бюджет от 3 000 ₽")
    assert not too_cheap("Ищу разработчика сайта, бюджет от 80 000 ₽")


def test_real_client_requests_pass():
    for text in (GOOD_BOT, GOOD_BITRIX, GOOD_CATALOG, GOOD_SMM, GOOD_INTRO):
        assert learned_block(text) is None, text
        assert not_my_job(text) is None, text
        assert intake_hits(text, _sources()), text
        assert judge(text)["fit"] >= 3, text


def _rules_reject(text: str) -> str | None:
    """Отказ правилами, без памяти уроков. Так виден свежий пост того же вида."""
    sources = _sources()
    stopped = hits_stopword(text, sources.get("stopwords") or [])
    if stopped:
        return f"stop:{stopped}"
    if too_cheap(text):
        return "cheap"
    return not_my_job(text)


def test_audit_lessons_are_rejected_by_rules():
    sources = _sources()
    for lead_id in AUDIT_IDS:
        text = _about(lead_id)
        assert text, lead_id
        reason = _rules_reject(text)
        assert reason, lead_id
        assert intake_hits(text, sources, assume_request=True) is None


def test_every_stored_lesson_repost_is_rejected():
    sources = _sources()
    leaked = []
    for row in _lessons():
        text = row.get("about") or ""
        if len(text) < 40:
            continue
        if intake_hits(text, sources, assume_request=True):
            leaked.append(row.get("lead_id"))
    assert leaked == []


# Тексты, которые правила не классифицируют: обфусцированный спам, SEO и точечная 1С,
# урезанный пост про систему новостей. Точный перепост всё равно режет learned_block.
KNOWN_RULE_GAPS = {4434, 5197, 5080, 4681, 5132, 5128, 5129, 5143}


def test_rule_gaps_stay_inside_known_lessons():
    missed = []
    for row in _lessons():
        text = row.get("about") or ""
        if text and not _rules_reject(text):
            missed.append(row.get("lead_id"))
    assert set(missed) <= KNOWN_RULE_GAPS, set(missed) - KNOWN_RULE_GAPS


def test_learned_block_uses_post_text_not_comment():
    about = _about(4313)
    assert learned_block(about)
    assert learned_block("это не заказ, а болтовня или реклама " * 3) is None
    fresh = add_like = {
        "reason": "это не заказ, а болтовня или реклама",
    }
    assert reason_class(fresh["reason"]) == "promo"
    assert add_like["reason"] not in (learned_block(GOOD_BOT) or "")


def test_reply_prompt_locks_voice_and_contacts():
    lead = {
        "text": "Нужна связка Битрикс24 и 1С для HR-процессов, бюджет 120 000",
        "sender_name": "Анна",
        "channel_title": "заказы",
    }
    prompt = _prompt(lead, "", "none", "", "")
    assert "pavel_AI_IT" not in prompt
    assert "t.me/pavel_AI_IT" not in prompt
    assert "@pasha_kvk" in prompt
    assert "@paxa_terminator" in prompt
    assert "https://pavelklyushin.ru/" in prompt
    assert "технический партнёр" in prompt
    assert "мы с командой" in prompt
    assert "аккумулятор" in prompt
    assert "вводных пока немного" in prompt
    assert "20 000" in prompt
    assert "не переносить" in prompt


def test_reply_checker_catches_wrong_voice_and_accepts_his():
    task = "Нужна связка Битрикс24 и 1С для HR-процессов"
    bad = (
        "Здравствуйте! Меня зовут Павел, я ИИ-специалист, мы с командой занимаемся продвижением.\n"
        "Жду вашего ответа.\n"
        "https://t.me/pavel_AI_IT"
    )
    good = (
        "Здравствуйте! Задача интересная, как раз плотно работаю со связкой Битрикс24 и 1С "
        "для HR-процессов.\n"
        "Так как вводных пока немного, предлагаю списаться и обсудить, какие процессы закрыть первыми.\n"
        "Я технический партнёр для бизнеса.\n"
        "https://pavelklyushin.ru/\n"
        "Telegram: @pasha_kvk\n"
        "Напишите, какой HR-процесс сейчас отнимает больше всего времени?"
    )
    assert reply_problems(bad, task)
    assert reply_problems(good, task) == []
