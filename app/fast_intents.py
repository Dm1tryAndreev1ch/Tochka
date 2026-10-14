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

MONTHS = dict(zip(
    ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"),
    range(1, 13), strict=True,
))
WEEKDAYS = {"понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3, "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5, "воскресенье": 6}


def simple_calendar_date(text, now, *, booking_horizon_days=None):
    """Preserve explicit dates; never infer next year or an ambiguous week."""
    value = text.lower().replace("ё", "е")
    if re.search(r"\b(?:не|или|до|после|через|спустя|кажд\w*|еженедель\w*|следующ\w*|прошл\w*)\b", value):
        return None
    candidates = []
    for word, delta in (("сегодня", 0), ("завтра", 1), ("послезавтра", 2)):
        if re.search(r"\b" + word + r"\b", value):
            candidates.append(now.date() + timedelta(days=delta))
    try:
        for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", value):
            candidates.append(datetime(int(match[1]), int(match[2]), int(match[3])).date())
        for match in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", value):
            candidates.append(datetime(int(match[3]), int(match[2]), int(match[1])).date())
        for match in re.finditer(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4})(?:\s+года?)?)?\b", value):
            candidate = datetime(int(match[3]) if match[3] else now.year, MONTHS[match[2]], int(match[1])).date()
            if not match[3] and not (
                booking_horizon_days is not None and 0 < (candidate - now.date()).days <= booking_horizon_days
            ):
                return None
            candidates.append(candidate)
    except ValueError:
        return None
    for match in re.finditer(r"\b(" + "|".join(WEEKDAYS) + r")\b", value):
        prefix = re.findall(r"\w+", value[:match.start()])
        if prefix and prefix[-1] not in {"в", "во", "на", "эту", "этот", "ближайшую", "ближайший"}:
            return None
        if re.search(r"\b(?:январ|феврал|март|апрел|ма[йяе]\b|июн|июл|август|сентябр|октябр|ноябр|декабр)", value):
            return None
        delta = WEEKDAYS[match[1]] - now.weekday()
        # Same day and an already passed weekday require a choice of week.
        if delta <= 0:
            return None
        candidates.append(now.date() + timedelta(days=delta))
    return candidates[0].isoformat() if len(candidates) == 1 else None


def awaiting_booking_time(context):
    return bool(
        context.get("intent") == "booking" and context.get("service_id")
        and context.get("date") and not context.get("time")
    )

def awaiting_customer_name(context):
    if context.get("name"):
        return False
    if context.get("intent") == "booking":
        return all(context.get(field) for field in ("service_id", "date", "time"))
    return context.get("intent") == "callback" and bool(context.get("message"))


def pending_customer_name(text, context, services):
    """Collect a short name only as the answer to the current name question."""
    if not awaiting_customer_name(context) or "?" in text:
        return None
    candidate = re.sub(r"[.!]+$", "", text.strip()).strip()
    candidate = re.sub(r"^я\s+", "", candidate, flags=re.IGNORECASE)
    plain_name = re.fullmatch(r"[А-ЯЁA-Zа-яёa-z]{2,30}(?:\s+[А-ЯЁA-Zа-яёa-z]{2,30}){0,2}", candidate)
    # Retain the prior capitalized-name path for hyphens and apostrophes.
    capitalized_name = re.fullmatch(
        r"[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}){0,2}", candidate,
    )
    if not (plain_name or capitalized_name):
        return None
    normalized = candidate.lower().replace("ё", "е")
    controls = {
        "да", "нет", "не", "спасибо", "отмена", "отмените", "позже", "потом", "скажу", "хочу",
        "передумал", "передумала", "подтверждаю", "записывайте", "согласен", "согласна", "верно",
        "хорошо", "ладно", "пожалуйста", "привет", "здравствуйте", "пока", "продолжим", "стоп",
        "запись", "запишите", "перезвоните", "телефон", "номер", "имя", "это", "можно", "нужно",
        "как", "какой", "какая", "какие", "кто", "где", "когда", "зачем", "почему", "что", "сколько",
        "вы", "вас", "вам", "мне", "меня", "мой", "у", "в", "на", "и", "или",
        "скажите", "расскажите", "объясните", "повторите", "уточните", "помогите", "адрес", "цена",
        "плюс", "без", "имени", "пропустим", "пропустите", "пропустить", "отказываюсь",
    }
    controls.update(word for phrase in HOURS for word in phrase.split())
    if controls.intersection(normalized.split()):
        return None
    if re.search(
        r"\b(?:сегодня|завтра|послезавтра|понедель|вторник|сред[ау]|четверг|пятниц|суббот|воскрес|"
        r"январ|феврал|март|апрел|ма[йяе]\b|июн|июл|август|сентябр|октябр|ноябр|декабр|"
        r"оператор|сотрудник|администратор|человек)", normalized,
    ) or relative_or_ordinal_date_spoken(normalized) or clock_time_spoken(normalized, pending=True):
        return None
    for service in services:
        stem = service["name"].lower().replace("ё", "е").strip()[:7]
        if stem and re.search(r"\b" + re.escape(stem) + (r"\b" if len(stem) < 4 else ""), normalized):
            return None
    # Fix ASR/all-caps casing while preserving intentionally mixed case names.
    return " ".join(
        word if word[0].isupper() and not word.isupper() else word.title()
        for word in candidate.split()
    )


def relative_or_ordinal_date_spoken(text):
    """Evidence for semantic date resolution, never a guessed calendar value."""
    if re.search(
        r"\b(?:через|спустя)\s+(?:(?:\d{1,2}|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+)?"
        r"(?:день|дня|дней|сутки|суток|неделю|недели|недель)\b", text,
    ):
        return True
    ordinal = (
        r"(?:первого|второго|третьего|четвертого|пятого|шестого|седьмого|восьмого|девятого|"
        r"десятого|одиннадцатого|двенадцатого|тринадцатого|четырнадцатого|пятнадцатого|"
        r"шестнадцатого|семнадцатого|восемнадцатого|девятнадцатого|двадцатого|тридцатого)"
    )
    # «В половине третьего» contains a clock ordinal, not a calendar day.
    calendar_text = re.sub(r"\b(?:половин[аеуы]|четверть)\s+" + ordinal + r"\b", "", text)
    return bool(re.search(r"\b(?:[1-9]|[12]\d|3[01])-?го\b|\b" + ordinal + r"\b", calendar_text))


def compound_clock_time_spoken(text, *, pending=False):
    """Recognize complete hour/minute evidence but leave conversion to the LLM."""
    hours = "|".join(re.escape(word) for word in sorted(HOURS, key=len, reverse=True))
    minute_words = [word for word, value in HOURS.items() if value < 20]
    ones = "|".join(word for word, value in HOURS.items() if 0 < value < 10)
    minutes = (
        r"(?:[0-5]?\d|(?:двадцать|тридцать|сорок|пятьдесят)(?:\s+(?:" + ones + r"))?|"
        + "|".join(sorted(minute_words, key=len, reverse=True)) + r")"
    )
    clock = (
        r"(?:(?:[01]?\d|2[0-3])|(?:" + hours + r"))\s+"
        r"(?:час(?:а|ов)?\s+(?:и\s+)?)?" + minutes
        + r"(?:\s+минут(?:а|ы|у)?)?(?:\s+(?:утра|дня|вечера|ночи))?"
    )
    ending = r"(?=\s*(?:$|[,.;!?]|(?:и|но|или|пожалуйста|на)\b))"
    for match in re.finditer(r"\b(?:в|на|к)\s+(" + clock + r")" + ending, text):
        if match[1] not in HOURS:
            return True
    match = re.fullmatch(r"\s*(" + clock + r")[.!?]?\s*", text) if pending else None
    return bool(match and match[1] not in HOURS)


def clock_time_spoken(text, *, pending=False):
    """Require an actual clock expression before permitting a generated time."""
    if re.search(r"(?<![\d:])(?:[01]?\d|2[0-3]):[0-5]\d\b", text):
        return True
    if compound_clock_time_spoken(text, pending=pending):
        return True
    hours = "|".join(re.escape(word) for word in sorted(HOURS, key=len, reverse=True))
    if pending and re.fullmatch(r"\s*(?:(?:[01]?\d|2[0-3])|(?:" + hours + r"))[.!?]?\s*", text):
        return True
    # A bare hour is only evidence when the phrase ends like a clock value.
    # In particular, «в один из дней», «на 16 октября» and «на два дня»
    # must not authorize the model to turn a vague date into a guessed time.
    ending = r"(?=\s*(?:$|[,.;!?]|(?:и|но|или|пожалуйста|на)\b))"
    numeric_minutes = r"(?:\s+[0-5]?\d)?(?![./]\d)"
    word_minutes = (
        r"(?:\s+(?:ноль|пять|десять|пятнадцать|двадцать|тридцать|сорок|пятьдесят)"
        r"(?:\s+(?:один|два|три|четыре|пять|шесть|семь|восемь|девять))?"
        r"(?:\s+минут(?:а|ы|у)?)?)?"
    )
    clock_suffix = r"(?:\s+час(?:а|ов)?)?(?:\s+(?:утра|дня|вечера|ночи))?" + ending
    if re.search(
        r"\b(?:в|на|к)\s+(?:(?:[01]?\d|2[0-3])\b" + numeric_minutes
        + r"|(?:" + hours + r")\b" + word_minutes + r")" + clock_suffix,
        text,
    ):
        return True
    ordinal = (
        r"(?:первого|второго|третьего|четвертого|пятого|шестого|седьмого|восьмого|"
        r"девятого|десятого|одиннадцатого|двенадцатого)"
    )
    return bool(re.search(
        r"\b(?:пол(?:день|ночь)|пол" + ordinal + r"|половин[аеуы]\s+" + ordinal
        + r"|четверть\s+" + ordinal + r"|без\s+четверти\s+(?:" + hours + r"))\b",
        text,
    ))


def simple_clock_time(text, *, pending=False):
    """Parse only complete unambiguous clocks; compound phrases go to the LLM."""
    values = [
        f"{int(match[1]):02}:{match[2]}"
        for match in re.finditer(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)\b", text)
    ]
    hours = "|".join(re.escape(word) for word in sorted(HOURS, key=len, reverse=True))
    hour_value = r"((?:[01]?\d|2[0-3])|(?:" + hours + r"))\b"
    suffix = r"(?:\s+час(?:а|ов)?)?(?=\s*(?:$|[,;!?]|\.(?!\d)|(?:и|но|или|пожалуйста|на)\b))"
    for match in re.finditer(r"\b(?:в|на|к)\s+" + hour_value + suffix, text):
        hour = int(match[1]) if match[1].isdigit() else HOURS[match[1]]
        values.append(f"{hour:02}:00")
    if pending and (match := re.fullmatch(r"\s*" + hour_value + r"[.!?]?\s*", text)):
        hour = int(match[1]) if match[1].isdigit() else HOURS[match[1]]
        values.append(f"{hour:02}:00")
    return values[0] if len(values) == 1 else None


def spoken_name(text, *, pending=False):
    person = r"(?!(?i:исправ\w*\b|замен\w*\b|помен\w*\b|телефон\b|номер\b))([А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}){0,2})"
    prefix = r"(?i:меня зовут|мо[её] имя|обращайтесь ко мне|называйте меня|(?:исправьте|замените|поменяйте)\s+имя\s+на|имя\s+(?:исправьте|замените|поменяйте)\s+на|имя[ :])\s*"
    if match := re.search(prefix + person, text):
        return match[1].strip()
    # A full name followed by a phone is also explicit, e.g. a callback request.
    if match := re.search(r"\b([А-ЯЁA-Z][а-яёa-z\-']{1,29}\s+[А-ЯЁA-Z][а-яёa-z\-']{1,29})\s*[,;]?\s*(?:\+\d|телефон)", text):
        return match[1].strip()
    if pending and re.fullmatch(r"[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}){0,2}", text.strip()):
        return text.strip()
    return None


def extract_common(text, context, services, now, *, booking_horizon_days=None):
    normalized = text.lower().replace("ё", "е")
    if mixed_request(text):
        return None
    if requests_human(text):
        return ExtractedIntent(intent="handoff")
    # A question mentioning a service or «запись звука» needs semantic analysis.
    # It must not silently become a booking or change the selected service.
    if re.search(
        r"\b(?:что|почему|как|где|когда|сколько|какой|какая|какие|можно|объясни|объясните|расскажи|расскажите|чем отличается)\b", normalized
    ) and not re.search(r"\b(?:записаться|запишите|записать|записывайте)\b", normalized):
        return None
    if re.search(r"\b(?:не хочу|не надо|не запис|не нужно|передумал)", normalized):
        return None
    if re.search(r"\bне\b|\b(?:или|любой|любая|любую)\b|которую обсуждали", normalized):
        return None
    booking = bool(re.search(r"\b(?:записаться|запишите|записать|записывай|хочу запись)\b", normalized))
    callback = bool(re.search(r"\b(?:перезвоните|перезвонить|обратный звонок)\b", normalized))
    intent = "callback" if callback else "booking" if booking else context.get("intent")
    if intent not in ("booking", "callback"):
        return None
    if intent == "booking" and re.search(r"\b(?:после|до|раньше|позже|между|примерно|около)\b", normalized):
        return None
    if len(re.findall(r"\b(?:сегодня|завтра|послезавтра)\b|\b\d{2}\.\d{2}\.\d{4}\b", normalized)) > 1 or text.count("+") > 1:
        return None
    if re.search(r"\b(?:добавоч\w*|внутренний\s+номер)\b", normalized):
        return None
    if re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", normalized):
        return None
    calendar_date = simple_calendar_date(text, now, booking_horizon_days=booking_horizon_days)
    # Unsupported or ambiguous calendar evidence must not become an hour/name.
    if not calendar_date and re.search(
        r"\b(?:январ|феврал|март|апрел|ма[йяе]\b|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
        r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|"
        r"\b\d{1,2}\.\d{1,2}\b(?!\.\d{4}\b)|"
        r"\b(?:понедель\w*|вторник\w*|сред[ау]|четверг\w*|пятниц\w*|суббот\w*|воскрес\w*)\b",
        normalized,
    ):
        return None
    if relative_or_ordinal_date_spoken(normalized):
        return None
    fields = {"intent": intent}
    if calendar_date:
        fields["date"] = calendar_date
    matched_services = []
    for service in services:
        stem = service["name"].lower().replace("ё", "е").strip()[:7]
        if stem and re.search(r"\b" + re.escape(stem) + (r"\b" if len(stem) < 4 else ""), normalized):
            matched_services.append(service["id"])
    if len(matched_services) > 1:
        return None
    if matched_services:
        fields["service_id"] = matched_services[0]
    if intent == "callback" and (message := re.fullmatch(r"\s*(?:вопрос|сообщение)\s*:\s*(.{3,1000})", text, flags=re.IGNORECASE)):
        fields["message"] = message[1].strip()
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
    pending_time = awaiting_booking_time(context)
    if compound_clock_time_spoken(normalized, pending=pending_time):
        return None
    if clock_time_spoken(normalized, pending=pending_time):
        value = simple_clock_time(normalized, pending=pending_time)
        if value is None:
            return None
        fields["time"] = value
    phone = re.search(r"\+[1-9][\d\s()\-]{7,60}(?!\w)", text)
    if phone:
        number = re.sub(r"[\s()\-]", "", phone[0])
        if re.fullmatch(r"\+[1-9]\d{7,14}", number):
            fields["phone"] = number
    elif number := spoken_digit_phone(text):
        fields["phone"] = number
    name = spoken_name(text)
    if name:
        fields["name"] = name
    elif len(fields) == 1 and (name := pending_customer_name(text, context, services)):
        fields["name"] = name
    # Do not merge a partial correction with the old value of an explicitly
    # mentioned field (especially a phone without an explicit country code).
    if context.get("phone") and not fields.get("message") and re.search(r"\b(?:телефон|номер|плюс)\b|\+", normalized) and not fields.get("phone"):
        return None
    if re.search(r"\b(?:исправ\w*|замен\w*|помен\w*|измен\w*)\b", normalized):
        for field, evidence in (("name", r"имя"), ("phone", r"телефон|номер"), ("date", r"дат\w*"), ("time", r"время"), ("service_id", r"услуг\w*")):
            if re.search(r"\b(?:" + evidence + r")\b", normalized) and not fields.get(field):
                return None
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
        )
    ):
        return None
    if len(fields) > 1 or booking or callback:
        return ExtractedIntent.model_validate(fields)
    return None


def requests_human(text):
    normalized = text.lower().replace("ё", "е")
    if re.search(r"\bне\s+(?:соедин|перевод|перевед|зов|вызыв|нужен|нужна|хочу)", normalized):
        return False
    person = r"(?:оператор\w*|сотрудник\w*|администратор\w*|человек\w*)"
    return bool(
        re.search(r"\b(?:соедини\w*|переведи\w*|перевести|позови\w*|пригласи\w*|дайте|поговорить|общаться)\b.{0,65}\b" + person, normalized)
        or re.search(r"\b(?:нужен|нужна|хочу|можно)\s+(?:жив\w*\s+)?" + person, normalized)
    )


def published_address(text, knowledge):
    if not re.search(r"\b(?:адрес|находитесь|расположены|добраться)\b", text.lower()):
        return None
    for document in knowledge:
        match = re.search(r"(?:^|[.!?]\s+)([^.!?]*\b[Аа]дрес[^.!?]*(?:[.!?]|$))", document["content"])
        if match:
            return match[1].strip()
    return None


def mixed_request(text):
    """Do not shortcut a compound request into just its first action."""
    value = text.lower().replace("ё", "е")
    booking = bool(re.search(r"\b(?:записаться|запишите|записать|записывайте)\b", value))
    callback = bool(re.search(r"\b(?:перезвон\w*|обратный звонок)\b", value))
    cancel = bool(re.search(r"\bотмен\w*\b", value))
    human = requests_human(text)
    facts = bool(re.search(
        r"\b(?:сколько|стоимост\w*|цен[ауые]|адрес\w*|телефон|контакт\w*|гаранти\w*|оплат\w*|"
        r"график|расписание|час[ыа] работы|услуг\w*|прайс)\b", value,
    )) and bool(re.search(r"\?|\b(?:како\w*|какие|где|когда|сколько|назовите|скажите|расскажите|покажите|перечислите)\b", value))
    extra_question = bool(re.search(r"\b(?:и|а еще|также)\s+(?:что|как|где|когда|почему|сколько|расскажи\w*|объясни\w*|скажите)\b", value))
    return sum((booking, callback, cancel, human)) > 1 or ((facts or extra_question) and any((booking, callback, cancel, human)))


def control_request(text, context, *, has_draft=False):
    """Return a requested control operation; the caller must check live state."""
    if mixed_request(text):
        return None
    normalized = " ".join(re.sub(r"[^а-яёa-z0-9\s]", "", text.lower()).split())
    if has_draft and normalized in {
        "да", "подтверждаю", "да подтверждаю", "всё верно", "все верно", "верно", "да всё верно",
        "да все верно", "согласен", "согласна", "записывайте",
    }:
        return "confirm"
    if has_draft or context.get("intent") in {"booking", "callback"}:
        if normalized in {"нет", "отмена", "не подтверждаю", "отмените", "передумал", "передумала", "я передумал", "я передумала"}:
            return "cancel_draft"
        if re.fullmatch(r"(?:отмени(?:те)?|убери(?:те)?|удали(?:те)?)\s+(?:мой\s+|текущий\s+)?черновик(?:\s+(?:записи|заявки))?", normalized):
            return "cancel_draft"
    return None


def social_reply(text, *, assistant_name="Геннадий", company_name="Точка", allowed_actions=None):
    """Only complete short social turns; no keyword match inside real requests."""
    normalized = " ".join(re.sub(r"[.!?,]", "", text.lower().replace("ё", "е")).split())
    if normalized in {"здравствуйте", "здравствуй", "привет", "добрый день", "доброе утро", "добрый вечер"}:
        return {"text": "Здравствуйте. Чем могу помочь?", "response_source": "system"}
    if normalized in {"спасибо", "спасибо вам", "большое спасибо", "благодарю", "благодарю вас"}:
        return {"text": "Пожалуйста. Если остались вопросы, я на связи.", "response_source": "system"}
    if normalized in {"до свидания", "пока", "всего доброго", "хорошего дня"}:
        return {"text": "До свидания. Хорошего дня!", "response_source": "system"}
    if normalized in {"ты здесь", "вы здесь", "ты меня слышишь", "вы меня слышите", "алло", "на связи"}:
        return {"text": "Я на связи. Задайте вопрос или скажите, чем помочь.", "response_source": "system"}
    if normalized in {"чем ты можешь помочь", "чем вы можете помочь", "что ты умеешь", "что вы умеете"}:
        actions = {"booking", "callback", "handoff"} if allowed_actions is None else set(allowed_actions)
        capabilities = ["рассказать об опубликованных услугах и правилах компании"]
        capabilities += [description for action, description in (
            ("booking", "помочь с записью"), ("callback", "принять заявку на обратный звонок"),
            ("handoff", "запросить подключение сотрудника"),
        ) if action in actions]
        return {"text": "Могу " + ", ".join(capabilities) + ".", "response_source": "system"}
    if normalized in {"как тебя зовут", "как вас зовут", "кто ты", "кто вы", "твое имя", "ваше имя"}:
        return {"text": f"Меня зовут {assistant_name}. Я виртуальный помощник компании {company_name}.", "response_source": "system"}
    return None


def spoken_digit_phone(text):
    """Only literal digits after an explicit plus; never infer a country code."""
    normalized = text.lower().replace("ё", "е").strip(" .!")
    match = re.fullmatch(r"(?:(?:мой\s+)?(?:телефон|номер)(?:\s+для связи)?\s*[: ]\s*)?(?:плюс|\+)\s*(.+)", normalized)
    if not match:
        return None
    tokens = [token for token in re.split(r"[\s,()-]+", match[1]) if token]
    digits = {word: str(number) for word, number in HOURS.items() if number < 10}
    if not tokens or any(not token.isdecimal() and token not in digits for token in tokens):
        return None
    number = "+" + "".join(token if token.isdecimal() else digits[token] for token in tokens)
    return number if re.fullmatch(r"\+[1-9]\d{7,14}", number) else None


def requested_slots(text, context, services, now):
    """A complete read-only availability lookup; unsupported constraints use the LLM."""
    value = text.lower().replace("ё", "е")
    if mixed_request(text) or re.search(r"\b(?:запиш\w*|запис\w*|перезвон\w*)\b", value) or not re.search(r"\bсвободн\w*\s+(?:врем\w*|окн\w*|слот\w*|мест\w*)\b", value):
        return None
    if re.search(r"\b(?:не|или|раньше|позже|после|до|утром|вечером)\b", value) or clock_time_spoken(value):
        return None
    ids = []
    for service in services:
        stem = service["name"].lower().replace("ё", "е")[:7]
        if len(stem) >= 4 and re.search(r"\b" + re.escape(stem), value):
            ids.append(service["id"])
    if len(ids) > 1:
        return None
    service_id = ids[0] if ids else context.get("service_id")
    if service_id not in {service["id"] for service in services}:
        return None
    date = simple_calendar_date(text, now)
    if not date:
        return None
    remainder = re.sub(
        r"\b(?:сегодня|завтра|послезавтра)\b|\b\d{1,2}\.\d{1,2}\.\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|"
        r"\b\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4}(?:\s+года?)?\b|\b(?:" + "|".join(WEEKDAYS) + r")\b",
        "", value,
    )
    allowed = {"есть", "ли", "у", "вас", "для", "на", "в", "эту", "этот", "ближайшую", "ближайший", "свободное", "свободные", "время", "окна", "слоты", "места", "какое", "какие", "покажите", "скажите", "подскажите", "пожалуйста", "доступно", "нужны", "хочу", "посмотреть"}
    if any(word not in allowed for word in service_request_words(remainder, services)):
        return None
    return {"service_id": service_id, "date": date}


def service_request_words(text, services):
    """Residual words expose unsupported qualifiers rather than silently losing them."""
    value = text.lower().replace("ё", "е")
    for service in services:
        stem = service["name"].lower().replace("ё", "е").strip()[:7]
        if stem:
            value = re.sub(r"\b" + re.escape(stem) + (r"\b" if len(stem) < 4 else r"\w*\b"), "", value)
    return re.findall(r"\w+", value)


def phone_correction_requested(text, context):
    """Explicit replacement in an existing business context, never a keep request."""
    if context.get("intent") not in {"booking", "callback"} or not context.get("phone"):
        return False
    value = text.lower().replace("ё", "е")
    phone = bool(re.search(r"\b(?:телефон|номер)\b|\+[1-9][\d ()-]{7,60}(?!\w)", value))
    if not phone or re.search(
        r"\bне\s+(?:(?:надо|нужно)\s+)?(?:меня\w*|измен\w*|помен\w*|замен\w*|обнов\w*|исправ\w*)\b|"
        r"\bне\s+(?:надо|нужно)\s+(?:(?:мой|этот|старый|прежний)\s+)?(?:телефон|номер)\s+(?:сейчас\s+)?(?:меня\w*|измен\w*|помен\w*|замен\w*|обнов\w*|исправ\w*)\b|"
        r"\b(?:меня\w*|измен\w*|помен\w*|замен\w*|обнов\w*|исправ\w*)\s+не\s+(?:надо|нужно)\b|"
        r"\bостав\w*\b.{0,30}\b(?:прежн\w*|стар\w*|телефон|номер)\b", value,
    ):
        return False
    if re.search(r"\b(?:какой|каков|назовите|скажите)\b", value) and not re.search(r"\b(?:исправ\w*|замен\w*|помен\w*|измен\w*|обнов\w*)\b", value):
        return False
    return bool(re.search(
        r"\b(?:исправ\w*|замен\w*|помен\w*|измен\w*|обнов\w*|новый|нового|теперь|вместо)\b|"
        r"\b(?:телефон|номер)\s+(?:не\s+)?(?:\+?\d|плюс\b)|\bне\s+\+[1-9][\d ()-]{7,60}", value,
    ))


def unresolved_phone_correction(text, context, extracted_phone):
    if not phone_correction_requested(text, context):
        return False
    if not extracted_phone:
        return True
    if extracted_phone != context["phone"]:
        return False
    # A model repeating the old number cannot resolve an explicitly different
    # literal or a rejection of that old number. Ask instead of reconfirming it.
    explicitly_repeated = False
    for match in re.finditer(r"\+[1-9][\d ()-]{7,60}(?!\w)", text):
        literal = re.sub(r"[ ()-]", "", match[0])
        if re.fullmatch(r"\+[1-9]\d{7,14}", literal):
            if literal != extracted_phone or re.search(r"\b(?:не|вместо)\s*$", text[:match.start()].lower()):
                return True
            explicitly_repeated = True
    return not explicitly_repeated
