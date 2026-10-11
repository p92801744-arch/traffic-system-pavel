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


def reason_class(reason: str) -> str:
    """Метка класса для калибровки. В текст поста как иголка не подставляется."""
    plain = (reason or "").lower().replace("ё", "е")
    if any(part in plain for part in ("болтовн", "реклам", "спам", "визит", "самореклам")):
        return "promo"
    if any(part in plain for part in ("штат", "сотрудник", "релокац")):
        return "staff"
    if any(part in plain for part in ("мелк", "чек", "разов")):
        return "cheap"
    if "не ии" in plain or "не автоматиза" in plain:
        return "off-topic"
    return "other"


def _norm_lite(text: str) -> str:
    lowered = (text or "").lower().replace("ё", "е")
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-zа-я0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


_SHINGLE_SKIP = _SKIP | _KEEP | {
    "будет", "будут", "можно", "нужно", "работа", "работы", "работу",
    "бизнес", "бизнеса", "бизнесу", "компании", "компания", "команда",
    "команды", "команде", "привет", "которые", "который", "которая",
    "через", "более", "сейчас", "также", "другие", "других", "клиент",
    "клиента", "клиентов", "задача", "задачи", "задачу", "проект",
    "проекта", "проекты", "человек", "человека", "людей", "нужен",
    "нужна", "нужны", "ищем", "ищут", "вакансия", "удаленно", "удаленка",
}


def content_words(text: str) -> list[str]:
    return [
        word for word in _words(_norm_lite(text))
        if len(word) >= 5 and word not in _SHINGLE_SKIP and not word.isdigit()
    ]


def about_shingles(text: str, n: int = 3) -> list[str]:
    """Характерные тройки из текста отклонённого поста, не из комментария Павла."""
    words = content_words(text)
    if len(words) < n:
        return []
    seen: list[str] = []
    for index in range(len(words) - n + 1):
        gram = " ".join(words[index:index + n])
        if gram not in seen:
            seen.append(gram)
    return seen


def _needles(reason: str) -> list[str]:
    """Оставлено для старых карточек. Фильтр эти иголки больше не применяет."""
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
    about_text = (about or "").strip()[:240]
    row = {
        "reason": reason,
        "lead_id": lead_id,
        "about": about_text,
        "conclusion": conclude(reason, about),
        "instruction": proposed_instruction(reason),
        "klass": reason_class(reason),
        "needles": _needles(reason),
        "ngrams": about_shingles(about_text)[:8],
        "at": _now(),
    }
    payload["lessons"].append(row)
    LESSONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return row


def records(limit: int | None = 30) -> list[dict]:
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
    ordered = list(reversed(rows))
    if limit is None:
        return ordered
    return ordered[:limit]


def learned_block(text: str) -> str | None:
    """Совпадение с текстом уже отклонённого поста, а не с комментарием Павла.

    Срабатывает на то же начало объявления или на две характерные тройки слов
    из поля about. Обобщённые причины («это не заказ, а болтовня») иголками не являются.
    """
    incoming_norm = _norm_lite(text)
    if len(incoming_norm) < 40:
        return None
    incoming = set(about_shingles(text))
    prefix = incoming_norm[:80]
    for row in records(None):
        about = str(row.get("about") or "")
        if len(about) < 40:
            continue
        about_norm = _norm_lite(about)
        if len(about_norm) >= 40 and about_norm[:80] == prefix:
            return row.get("conclusion") or row.get("reason")
        shared = incoming & set(about_shingles(about))
        if len(shared) >= 2:
            return row.get("conclusion") or row.get("reason")
    return None
