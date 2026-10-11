from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.settings import ROOT

LESSONS_PATH = ROOT / "data" / "reject-lessons.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_lessons(limit: int = 30) -> list[str]:
    if not LESSONS_PATH.exists():
        return []
    try:
        data = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = data.get("lessons") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    reasons: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            reasons.append(item.strip())
        elif isinstance(item, dict) and item.get("reason"):
            reasons.append(str(item["reason"]).strip())
    # последние важнее
    uniq: list[str] = []
    for reason in reversed(reasons):
        if reason not in uniq:
            uniq.append(reason)
    return uniq[:limit]


_SKIP = {
    "это", "этот", "эта", "эти", "такие", "такой", "такая", "заказ", "заказы",
    "заявка", "заявки", "подходит", "подходят", "нужен", "нужна", "нужно",
    "просто", "больше", "вообще", "очень", "меня", "мне", "моё", "мое",
    "для", "или", "как", "что", "его", "она", "они", "был", "была", "про",
    "там", "вот", "ещё", "еще", "ищет", "ищут", "только", "когда", "если",
    "тебе", "меня", "павел",
}
# Служебные слова услуг не становятся запретом: иначе отказ «не бот» закроет все боты.
_KEEP = {
    "бот", "бота", "боты", "агент", "агента", "нейросеть", "нейросети",
    "автоматизация", "автоматизацию", "chatgpt", "n8n",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9-]{4,}", (text or "").lower().replace("ё", "е"))


def _needles(reason: str) -> list[str]:
    plain = (reason or "").strip().lower().replace("ё", "е")
    clause = re.split(r"[,.!/]", plain)[0].strip()
    needles: list[str] = []
    if 10 <= len(clause) <= 70 and not clause.startswith("не "):
        needles.append(clause)
    tokens = [
        w for w in _words(plain)
        if w not in _SKIP and w not in _KEEP and not w.startswith("автоматиза")
    ]
    if len(tokens) >= 2:
        needles.append(" ".join(tokens[:3]))
    elif len(tokens) == 1 and len(tokens[0]) >= 6:
        needles.append(tokens[0])
    uniq: list[str] = []
    for item in needles:
        if item and item not in uniq:
            uniq.append(item)
    return uniq[:3]


def proposed_instruction(reason: str) -> str:
    comment = (reason or "").strip().rstrip(".")
    return f"Не класть в ленту, если Павел пишет: «{comment}»."


def conclude(reason: str, about: str) -> str:
    topic = re.sub(r"\s+", " ", (about or "").strip())
    if len(topic) > 140:
        topic = topic[:140] + "…"
    comment = (reason or "").strip()
    if topic:
        return f"Заказ был про «{topic}». Павел написал: «{comment}». Такие заявки больше не класть в ленту."
    return f"Павел написал: «{comment}». Такие заявки больше не класть в ленту."


def add_lesson(reason: str, lead_id: int | None = None, about: str = "") -> dict | None:
    reason = (reason or "").strip()
    if not reason:
        return None
    LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"lessons": []}
    if LESSONS_PATH.exists():
        try:
            loaded = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("lessons"), list):
                payload = loaded
        except json.JSONDecodeError:
            pass
    row = {
        "reason": reason,
        "lead_id": lead_id,
        "about": (about or "").strip()[:240],
        "conclusion": conclude(reason, about),
        "instruction": proposed_instruction(reason),
        "needles": _needles(reason),
        "at": _now(),
    }
    payload["lessons"].append(row)
    LESSONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return row


def records(limit: int = 30) -> list[dict]:
    if not LESSONS_PATH.exists():
        return []
    try:
        data = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = data.get("lessons") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    rows = [item for item in items if isinstance(item, dict) and item.get("reason")]
    return list(reversed(rows))[:limit]


def learned_block(text: str) -> str | None:
    """Если комментарий Павла уже закрыл такую заявку — вернуть вывод."""
    plain = (text or "").lower().replace("ё", "е")
    for row in records(40):
        for needle in row.get("needles") or []:
            if needle and needle in plain:
                return row.get("conclusion") or row.get("reason")
        reason = str(row.get("reason") or "")
        tokens = [w for w in _words(reason) if w not in _SKIP and w not in _KEEP][:3]
        if len(tokens) >= 2 and all(token in plain for token in tokens):
            return row.get("conclusion") or reason
    return None
