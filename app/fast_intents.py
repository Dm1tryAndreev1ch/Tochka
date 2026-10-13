"""Conservative Russian parsing for common receptionist phrases. Unknowns go to the LLM."""

import re
from datetime import datetime, timedelta

from app.schemas import ExtractedIntent

HOURS = {
    "ноль": 0,
    "один": 1,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "двадцать один": 21,
    "двадцать два": 22,
    "двадцать три": 23,
}


def extract_common(text, context, services, now):
    normalized = text.lower().replace("ё", "е")
    # A question mentioning a service or «запись звука» needs semantic analysis.
    # It must not silently become a booking or change the selected service.
    if re.search(
        r"\b(?:что|почему|объясни|объясните|расскажи|расскажите|чем отличается)\b", normalized
    ) and not re.search(r"\b(?:записаться|запишите|записать|записывайте)\b", normalized):
        return None
    if re.search(r"\b(?:не хочу|не надо|не запис|не нужно|передумал)", normalized):
        return None
    booking = bool(re.search(r"\b(?:записаться|запишите|записать|записывай|запись)\b", normalized))
    callback = bool(re.search(r"\b(?:перезвоните|перезвонить|обратный звонок)\b", normalized))
    intent = "callback" if callback else "booking" if booking else context.get("intent")
    if intent not in ("booking", "callback"):
        return None
    fields = {"intent": intent}
    for service in services:
        stem = service["name"].lower().replace("ё", "е")[:7]
        if len(stem) >= 4 and stem in normalized:
            fields["service_id"] = service["id"]
            break
    today = now.date()
    if re.search(r"\bпослезавтра\b", normalized):
        fields["date"] = (today + timedelta(days=2)).isoformat()
    elif re.search(r"\bзавтра\b", normalized):
        fields["date"] = (today + timedelta(days=1)).isoformat()
    elif re.search(r"\bсегодня\b", normalized):
        fields["date"] = today.isoformat()
    full = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", normalized)
    if full:
        try:
            fields["date"] = datetime(int(full[3]), int(full[2]), int(full[1])).date().isoformat()
        except ValueError:
            return None
    explicit = re.search(r"\b(?:в|на)\s+(\d{1,2})(?::(\d{2}))?(?:\s*(?:час(?:а|ов)?))?(?![\d.])", normalized)
    if explicit:
        if explicit[2] is None and re.match(r"\s+\d", normalized[explicit.end() :]):
            return None
        hour = int(explicit[1])
        minute = int(explicit[2] or 0)
        if hour > 23 or minute > 59:
            return None
        fields["time"] = f"{hour:02}:{minute:02}"
    else:
        for word, hour in sorted(HOURS.items(), key=lambda pair: -len(pair[0])):
            if re.search(r"\b(?:в|на)\s+" + word + r"\b", normalized):
                fields["time"] = f"{hour:02}:00"
                break
    phone = re.search(r"\+[1-9][\d\s()\-]{7,60}(?![\d])", text)
    if phone:
        number = re.sub(r"[\s()\-]", "", phone[0])
        if re.fullmatch(r"\+[1-9]\d{7,14}", number):
            fields["phone"] = number
    name = re.search(
        r"(?i:меня зовут|мое имя|моё имя|имя[ :])\s*([А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-\']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-\']{1,29}){0,2})",
        text,
    )
    if name:
        fields["name"] = name[1].strip()
    elif context.get("intent") and not context.get("name") and len(fields) == 1:
        if re.fullmatch(
            r"[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-\']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-\']{1,29}){0,2}",
            text.strip(),
        ):
            fields["name"] = text.strip()
    # A time phrase must be complete. Do not turn «полчетвёртого» or «после
    # обеда» into a guessed time or discard an unparsed correction.
    if any(
        term in normalized
        for term in (
            "полчет",
            "половин",
            "без четвер",
            "после обеда",
            "вечер",
            "утра",
            "минут",
            "следующ",
            "понедель",
            "вторник",
            "среду",
            "четверг",
            "пятниц",
            "суббот",
            "воскрес",
        )
    ):
        return None
    if len(fields) > 1 or booking or callback:
        return ExtractedIntent.model_validate(fields)
    return None


def published_address(text, knowledge):
    if not re.search(r"\b(?:адрес|находитесь|расположены|добраться)\b", text.lower()):
        return None
    for document in knowledge:
        match = re.search(r"(?:^|[.!?]\s+)([^.!?]*\b[Аа]дрес[^.!?]*(?:[.!?]|$))", document["content"])
        if match:
            return match[1].strip()
    return None
