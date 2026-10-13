"""Kwork: берём только заказы с числом предложений не больше порога."""
from __future__ import annotations

import json

from app import kwork


def _page(items):
    return "<script>window.x={" + '"wantsListData":' + json.dumps({"pagination": {"data": items}}) + "}</script>"


def _item(pid, offers):
    row = {"id": pid, "name": "Сайт на WordPress", "description": "Нужен каталог", "possiblePriceLimit": 30000,
           "date_active": "2026-10-13 09:00:00"}
    if offers is not ...:
        row["kwork_count"] = offers
    return row


def test_offers_threshold_and_unknown_kept():
    html = _page([_item(1, 0), _item(2, 9), _item(3, 10), _item(4, 23), _item(5, ...), _item(6, None)])
    ids = [row["message_id"] for row in kwork._orders(html, 9)]
    assert ids == [1, 2, 5, 6]
    assert len(kwork._orders(html)) == 6


def test_offers_count_in_text():
    row = kwork._orders(_page([_item(7, 3)]), 9)[0]
    assert row["offers"] == 3
    assert "Предложений на Kwork: 3" in row["text"]


def test_threshold_from_profile():
    assert kwork.max_offers() == 9
