"""Company facts are source excerpts; a fluent model answer is not evidence."""

import re

MISSING_FACT = "Эти сведения не опубликованы в базе компании. Могу передать вопрос сотруднику."


def fact_sources(services, knowledge):
    return [doc["content"] for doc in knowledge] + [
        f"{s['name']}: {s['description']} Цена — {s['price']} BYN. Продолжительность — {s['duration']} минут."
        for s in services
    ]


def phone_question(text):
    if re.search(r"\b(?:мой|мои|мне)\b", text.lower()):
        return False
    if re.search(r"\+\d|\bплюс\b", text.lower()) and not re.search(r"\b(?:какой|каков|назовите|скажите|ваш|компании)\b", text.lower()):
        return False
    return bool(re.search(
        r"(?:какой|каков|назовите|скажите|ваш|компании|для связи|связаться|позвонить).*?(?:телефон|номер|контакт)|"
        r"(?:телефон|номер|контакт).*?(?:компании|ваш|для связи)|как\s+(?:вам|с вами)\s+(?:позвонить|связаться)",
        text.lower(),
    ))


def phone_answer(text, knowledge):
    if not phone_question(text):
        return None
    for doc in knowledge:
        for sentence in re.split(r"(?<=[.!?])\s+|\n", doc["content"]):
            if re.search(r"телефон|контакт|для связи", sentence.lower()):
                phone = re.search(r"\+[1-9][\d ()-]{7,30}", sentence)
                if phone and re.fullmatch(r"\+[1-9]\d{7,14}", re.sub(r"[ ()-]", "", phone[0])):
                    return {"text": sentence.strip(), "response_source": "knowledge"}
    return {"text": MISSING_FACT, "response_source": "missing_fact"}


def grounded_reply(extracted, text, services, knowledge):
    sources = fact_sources(services, knowledge)
    company = extracted.intent == "faq" or bool(re.search(
        r"\b(?:компани\w*|адрес|стоимост\w*|цен\w*|скидк\w*|гаранти\w*|оплат\w*)\b",
        text.lower(),
    )) or bool(re.search(r"\b(?:компания|у нас|наши услуги|наш адрес|наша цена)\b", extracted.answer.lower()))
    if company:
        quotes = extracted.fact_quotes
        if quotes and all(quote.strip() and any(quote in source for source in sources) for quote in quotes):
            return {"text": " ".join(dict.fromkeys(quotes)), "response_source": "knowledge"}
        return {"text": MISSING_FACT, "response_source": "missing_fact"}
    if re.search(
        r"(?:запись|заявка|оплата)\s+(?:подтверждена|сохранена|выполнена|прошла)|"
        r"(?:записал|записала|соединил|соединила|перевёл|перевела|позвонил|позвонила)\s+(?:вас|вам|сотруднику)",
        extracted.answer.lower(),
    ):
        return {"text": "Для выполнения действия нужно отдельное подтверждение. Уточните, что вы хотите сделать."}
    available = " ".join(sources) + " " + text
    numbers = re.findall(r"\+?\d[\d ()-]{1,30}\d", extracted.answer)
    if any(re.sub(r"[ ()-]", "", number) not in re.sub(r"[ ()-]", "", available) for number in numbers):
        return {"text": MISSING_FACT, "response_source": "missing_fact"}
    return {"text": extracted.answer or "Уточните, пожалуйста, вопрос. Я могу рассказать об услугах, записать вас или оставить заявку сотруднику."}


def faq_topic(text):
    """Recognize one supported question; qualifiers must not get silently lost."""
    from app.fast_intents import mixed_request

    value = text.lower().replace("ё", "е")
    # Personal edits and transaction status are not questions about company facts.
    if re.search(
        r"\b(?:мой|моя|мое|мою|мои|моего|моей|моем|исправ\w*|замен\w*|помен\w*|измен\w*|обнов\w*|"
        r"отмен\w*|удал\w*|оплатил\w*|заплатил\w*|платеж\w*|списал\w*|списан\w*|ошиб\w*|неверн\w*|"
        r"отклон\w*|чек|квитанц\w*|статус)\b|\bне\s+(?:работает|прошла|прошел|проходит)\b", value,
    ):
        return None
    one_facet = re.sub(r"наличными\s+и\s+(?:банковской\s+)?картой|суббот[ауе]\s+и\s+воскресень[ея]", "", value)
    if mixed_request(text) or text.count("?") > 1 or re.search(
        r"\b(?:не надо|не нужно|не спрашиваю|забудьте|и|также|а еще)\b", one_facet,
    ):
        return None
    topics = set()
    email = bool(re.search(r"\b(?:email|e-mail|почт\w*|написать\s+вам\s+письмо)\b", value))
    if email:
        topics.add("email")
    if not email and re.search(r"\b(?:адрес|находитесь|расположен\w*|добраться)\b", value):
        topics.add("address")
    if phone_question(text):
        topics.add("phone")
    if re.search(r"\b(?:выходн\w*|суббот\w*|воскресень\w*)\b", value) and re.search(r"работ|открыт|прийти|можно", value):
        topics.add("weekends")
    elif re.search(r"час[ыа] работы|график\s+работ|расписание\s+работ|когда.{0,15}(?:работ|открыт)|(?:до|во)\s+скольк.{0,20}(?:работ|откры|закрыв)|когда\s+вы\s+работ|^до\s+скольки[?.!]*$", value):
        topics.add("hours")
    if re.search(r"\b(?:оплат\w*|карт\w*|наличн\w*|способ\w*\s+оплаты)\b", value):
        if re.search(r"\b(?:комисси\w*|скидк\w*|рассроч\w*|крипт\w*|перевод\w*|доллар\w*|евро|возврат\w*|аванс\w*|предоплат\w*|онлайн|счет\w*|мир|visa|mastercard)\b", value):
            return None
        topics.add("payment")
    if re.search(r"\bгаранти\w*\b", value):
        topics.add("warranty")
    if re.search(r"\b(?:прайс|перечислите\s+услуги|список\s+услуг|какие.{0,15}услуги)\b", value):
        topics.add("services")
    elif re.search(r"\b(?:цен[ауые]|стоимость|стоит|стоят)\b", value):
        topics.add("price")
    if "warranty" not in topics and re.search(r"\b(?:длится|длительность|продолжительность|сколько\s+времени|сколько\s+минут)\b", value):
        topics.add("duration")
    if re.search(r"\b(?:праздник\w*|сегодня|завтра|ночью|двор\w*|вход(?:а|е|у|ом)?|парков\w*|скидк\w*|сайт\w*|юридическ\w*|почтовый|филиал\w*)\b", value):
        return None
    return next(iter(topics)) if len(topics) == 1 else None


def fast_faq_reply(text, services, knowledge, schedule=None):
    """Return current exact public excerpts, or leave unsupported detail to the LLM."""
    topic = faq_topic(text)
    if topic is None:
        return None
    value = text.lower().replace("ё", "е")
    matched = []
    for service in services:
        stem = service["name"].lower().replace("ё", "е").strip()[:7]
        if stem and re.search(r"\b" + re.escape(stem) + (r"\b" if len(stem) < 4 else ""), value):
            matched.append(service)
    if topic == "weekends" and matched and not re.search(r"работ|открыт", value):
        return None
    if topic in {"services", "price", "duration"}:
        from app.fast_intents import service_request_words

        allowed = {
            "сколько", "стоит", "стоят", "стоимость", "стоимостью", "цену", "цена", "цены", "ценами", "цене",
            "какая", "какой", "какие", "какова", "каков", "каковы", "ли", "долго", "длится", "длительность",
            "продолжительность", "времени", "минут", "занимает", "займет", "скажите", "подскажите", "назовите",
            "расскажите", "перечислите", "покажите", "пожалуйста", "у", "вас", "в", "на", "за", "по", "с", "мне",
            "прайс", "список", "услуг", "услуги", "услуга", "есть", "что", "входит", "вы", "оказываете", "предоставляете",
        }
        if any(word not in allowed for word in service_request_words(text, services)):
            return None
        if topic != "services" and not matched:
            return None
        chosen = services if topic == "services" else matched
        if not chosen:
            return None
        return {"text": " ".join(fact_sources(chosen, [])), "response_source": "knowledge"}
    if topic == "hours" and schedule is not None:
        days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return {
            "text": f"Работаем с {schedule['opens']:02}:00 до {schedule['closes']:02}:00 по Минску. Дни: "
            + ", ".join(days[day] for day in schedule["weekdays"]) + ".",
            "response_source": "knowledge",
        }
    if topic == "weekends" and schedule is not None:
        times = f"работаем с {schedule['opens']:02}:00 до {schedule['closes']:02}:00 по Минску"
        return {
            "text": " ".join(
                f"В {name} {times if day in schedule['weekdays'] else 'не работаем'}."
                for day, name in ((5, "субботу"), (6, "воскресенье"))
            ),
            "response_source": "knowledge",
        }
    sentences = [sentence.strip() for doc in knowledge for sentence in re.split(r"(?<=[.!?])\s+|\n", doc["content"]) if sentence.strip()]
    selectors = {
        "address": r"\bадрес\s*:",
        "email": r"(?:[a-z0-9_.+-]+@[a-z0-9.-]+\.[a-z]{2,})",
        "phone": r"\b(?:телефон|для связи|контакт)\b.*\+[1-9][\d ()-]{7,30}",
        "hours": r"(?:работ|открыт).*(?:\d{1,2}:\d{2}|\d{1,2}\s*час)",
        "weekends": r"\b(?:суббот\w*|воскресень\w*|выходн\w*)\b.*(?:работ|открыт)|(?:работ|открыт).*\b(?:суббот\w*|воскресень\w*|выходн\w*)\b",
        "payment": r"\b(?:оплат\w*|принима\w*)\b.*(?:наличн\w*|карт\w*)",
        "warranty": r"\bгаранти\w*\b",
    }
    excerpts = [sentence for sentence in sentences if re.search(selectors[topic], sentence.lower().replace("ё", "е"))]
    if topic == "warranty" and matched:
        stems = [service["name"].lower().replace("ё", "е")[:7] for service in matched]
        excerpts = [sentence for sentence in excerpts if any(stem in sentence.lower().replace("ё", "е") for stem in stems)]
    if not excerpts and topic in {"phone", "email", "address"}:
        return {"text": MISSING_FACT, "response_source": "missing_fact"}
    if not excerpts or (topic == "address" and len(set(excerpts)) > 1):
        return None
    # Return all matches rather than selecting one potentially conflicting revision.
    # Conflicting facts require semantic review, not an arbitrary first sentence.
    if len(excerpts) > 3 or sum(len(sentence) for sentence in excerpts) > 900:
        return None
    return {"text": " ".join(dict.fromkeys(excerpts)), "response_source": "knowledge"}
