"""Jev (TypeSafe System One) — быстрая типизированная оценка заявки."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.settings import settings

log = logging.getLogger("leads.jev")

PROFILE_BLURB = (
    "Pavel Klyushin (Russia) is a solo technical partner for business, a hands-on engineer. "
    "He says «я», never «мы с командой». Paid project work: websites and catalogs, "
    "n8n/Make/API automation, 1C and CRM (including Bitrix24), AI agents and bots, "
    "knowledge bases, product-card pipelines. "
    "SMM only if the budget is at least 50000 RUB AND the post names the channel and the system "
    "(Dzen, auto-posting, content pipeline, traffic system). "
    "Reject: reviews and case bragging, freelancer self-promo and business cards, "
    "service listings (сделаю / от N ₽ за), staff hiring, tiny one-off edits, "
    "magic-button requests, long tenders, copywriting-only or vague SMM, "
    "marketplace managers, courses, spam, crypto."
)


def enabled() -> bool:
    return bool(settings.jev_key)


async def decide(state: str | dict, questions: dict[str, Any]) -> dict[str, Any]:
    key = settings.jev_key
    if not key:
        raise RuntimeError("JEV_API_KEY пустой")
    base = (settings.jev_base_url or "https://api.typesafe.ai").rstrip("/")
    url = f"{base}/v1/systemone"
    payload = {
        "model": settings.jev_model or "jev-latest",
        "state": state,
        "questions": questions,
    }
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Jev HTTP {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    if not isinstance(data, dict) or "answers" not in data:
        raise RuntimeError(f"Jev странный ответ: {str(data)[:300]}")
    return data


def _fit_questions() -> dict[str, Any]:
    return {
        "fit": {
            "type": "score",
            "instructions": (
                "How well should Pavel take this lead for paid work matching his services? "
                + PROFILE_BLURB
            ),
            "criteria": [
                "1 — Not a fit: spam, self-promo, staff vacancy, wrong role, or no paid task",
                "2 — Weak: client-like but not his services",
                "3 — Possible: related AI/automation/web, but thin or unclear",
                "4 — Good: clear paid ask for his services",
                "5 — Excellent: owner/high-ticket, repeating process, core offer",
            ],
        },
        "is_client_order": {
            "type": "noul",
            "instructions": (
                "The author is a paying client asking for work to be done "
                "(not a freelancer advertising themselves, not a staff vacancy)."
            ),
        },
        "marketplace_cards_ok": {
            "type": "noul",
            "instructions": (
                "If the post is about marketplaces (WB/Ozon), it is specifically about "
                "creating product cards / catalog pipeline — not hiring a marketplace manager "
                "and not one-off slide design."
            ),
        },
    }


def _score_to_fit(raw: float | int | None) -> int:
    """Score criteria are 5 levels → weighted value roughly 0..4."""
    if raw is None:
        return 2
    # Round to nearest level, then map 0..4 → fit 1..5
    level = int(round(float(raw)))
    return max(1, min(5, level + 1))


async def score_lead_text(text: str, channel: str | None = None) -> dict:
    """Возвращает тот же словарь, что и локальный judge."""
    channel = (channel or "").strip()
    state = {
        "channel": channel or "unknown",
        "post": (text or "")[:8000],
        "who_is_pavel": PROFILE_BLURB,
    }
    data = await decide(state, _fit_questions())
    answers = data.get("answers") or {}
    fit_ans = answers.get("fit") or {}
    client_ans = answers.get("is_client_order") or {}
    cards_ans = answers.get("marketplace_cards_ok") or {}

    fit = _score_to_fit(fit_ans.get("score"))
    conf = float(fit_ans.get("confidence") or 0)
    client_p = float(client_ans.get("noul") or 0)
    cards_p = float(cards_ans.get("noul") or 0)

    # Неуверенность Jev → чуть понижаем, чтобы не заспамить бот слабыми.
    if conf < 0.35 and fit >= 4:
        fit = 3
    if client_p < 0.35 and fit >= 3:
        fit = min(fit, 2)

    paid = client_p >= 0.55 and fit >= 3
    high = fit >= 4

    if fit <= 2:
        why = (
            f"Jev почти не отдавал ({fit}/5, уверенность {conf:.0%}): "
            f"похоже не на твой платный заказ (вероятность заказчика {client_p:.0%})."
        )
        offer = "не подходит"
    else:
        why = (
            f"Jev отдал {fit}/5 (уверенность {conf:.0%}): "
            f"похоже на заказчика ({client_p:.0%})"
        )
        if cards_p >= 0.6:
            why += "; по маркетплейсу — скорее карточки товара"
            offer = "карточки для Wildberries или Ozon, как в кейсе каталога"
        else:
            offer = "разбор в Telegram: бот, база знаний, 1С, каталог или Дзен — что ближе к боли"
        why += "."

    return {
        "fit": fit,
        "is_paid_order": paid,
        "high_ticket": high,
        "why": why,
        "suggested_offer": offer,
    }
