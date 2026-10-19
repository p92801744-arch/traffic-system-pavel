"""SMM-заявки шире (2026-10-19): ведение, продвижение, рилс и сторис проходят, мусор режется."""

from __future__ import annotations

import pytest

from app.filters import intake_hits, load_sources, not_my_job
from app.scorer import judge

GOOD = [
    "Ищу SMM-специалиста на ведение Instagram и Telegram для салона красоты. Нужны рилсы, сторис, контент-план. Бюджет 60 000 ₽ в месяц, ГПХ.",
    "Ищем таргетолога для продвижения группы ВК мебельной фабрики. Пишите с примерами кейсов.",
    "Ищем SMM-менеджера для продвижения Telegram-канала онлайн-школы. Пришлите портфолио и стоимость. Бюджет до 70к",
    "Нужен специалист на ведение и продвижение ВКонтакте для стоматологии, бюджет 45 000 руб в месяц",
]

JUNK = [
    "Ищу SMM-специалиста на ведение Instagram, бюджет 5000 ₽ в месяц",
    "Ищем SMM-специалиста для ведения соцсетей фонда, пока без оплаты, за отзыв и портфолио",
    "Ищу смм для продвижения ресторана в телеграм, по бартеру, ужин на двоих",
    "Я SMM-специалист, беру на ведение 2 проекта. Ищу новых клиентов: ведение Instagram и ВК, рилс, сторис.",
    "Есть 2 свободных места на ведение аккаунтов! Продвижение в Instagram, таргет ВК, нужен только ваш запрос.",
    "#Ищу #Сторисмейкера для продвижения аккаунта интернет-магазина.",
]


@pytest.mark.parametrize("text", GOOD)
def test_smm_leads_pass_and_score_three_plus(text):
    assert intake_hits(text, load_sources()), not_my_job(text)
    assert judge(text)["fit"] >= 3


@pytest.mark.parametrize("text", JUNK)
def test_smm_junk_is_cut(text):
    assert not intake_hits(text, load_sources())


def test_smm_between_floors_is_low():
    text = "Нужен смм-щик на ведение аккаунта кофейни в инсте, рилс 3 в неделю, оплата 25 000 р"
    assert intake_hits(text, load_sources())
    assert judge(text)["fit"] == 2


def test_bot_order_still_top():
    text = "Ищу разработчика чат-бота для интернет-магазина на n8n, бюджет 80 000 ₽"
    assert judge(text)["fit"] >= 4
