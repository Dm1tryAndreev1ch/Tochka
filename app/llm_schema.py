"""Constrain model output to the fields the chosen dialogue path can use."""

import copy
import re

from app.fast_intents import (
    awaiting_booking_time,
    awaiting_customer_name,
    clock_time_spoken,
    relative_or_ordinal_date_spoken,
    spoken_name,
)
from app.schemas import ExtractedIntent


def intent_schema(text, context, services, categories):
    fields = copy.deepcopy(ExtractedIntent.model_json_schema()["properties"])
    normalized = text.lower().replace("ё", "е")
    pending_time = awaiting_booking_time(context)
    time_spoken = clock_time_spoken(normalized, pending=pending_time)
    phone_spoken = bool(re.search(r"\+[1-9][\d ()-]{6,60}|\bплюс\s+(?:\d|один|два|три|четыр|пять|шест|сем|восем|девят)", normalized))
    explicit_phones = list(re.finditer(r"\+([1-9][\d ()-]{6,60})", text))
    positive_phones = []
    for match in explicit_phones:
        number = "+" + re.sub(r"[ ()-]", "", match[1])
        if re.fullmatch(r"\+[1-9]\d{7,14}", number) and not re.search(
            r"\b(?:не|вместо)\s*$", text[max(0, match.start() - 25):match.start()].lower(),
        ):
            if number not in positive_phones:
                positive_phones.append(number)
    if explicit_phones and not positive_phones and not re.search(r"\bплюс\b", normalized):
        phone_spoken = False
    date_spoken = bool(re.search(
        r"\b(?:сегодня|завтра|послезавтра|понедель|вторник|сред[ау]|четверг|пятниц|суббот|воскрес|"
        r"январ|феврал|март|апрел|ма[йяе]\b|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
        r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b|"
        r"через\s+.{1,25}\s+(?:дн|недел)|следующ\w*\s+недел", normalized,
    ))
    date_spoken |= relative_or_ordinal_date_spoken(normalized)
    pending_name = awaiting_customer_name(context) and not (date_spoken or time_spoken or phone_spoken)
    explicit_name = spoken_name(text, pending=pending_name)
    # Lower-case ASR/text names and «я Анна» are candidates for semantic
    # extraction, not literal values that the grammar must force into a name.
    name_spoken = bool(explicit_name) or bool(
        pending_name and re.fullmatch(
            r"(?i:я\s+)?[А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,29}"
            r"(?:\s+[А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,29}){0,2}[.!?]?",
            text.strip(),
        )
    )
    # Never expose the phone/name/date/time grammar when that datum wasn't
    # spoken. Existing state is retained by the server, rather than regenerated.
    spoken_fields = [field for field, spoken in (
        ("name", name_spoken), ("phone", phone_spoken), ("date", date_spoken), ("time", time_spoken),
    ) if spoken]
    matched_services = []
    for service in services:
        stem = service["name"].lower().replace("ё", "е").strip()[:7]
        if stem and re.search(r"\b" + re.escape(stem) + (r"\b" if len(stem) < 4 else ""), normalized):
            matched_services.append(service["id"])
    if matched_services:
        fields["service_id"] = {"type": "string", "enum": matched_services}
    required_spoken = []
    if explicit_name:
        fields["name"] = {"type": "string", "enum": [explicit_name]}
        required_spoken.append("name")
    if positive_phones:
        fields["phone"] = {"type": "string", "enum": positive_phones}
        if len(positive_phones) == 1:
            required_spoken.append("phone")
    else:
        fields["phone"] = {"type": "string", "pattern": r"^\+[1-9][0-9]{7,14}$"}
    fields["date"] = {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    fields["time"] = {"type": "string", "pattern": r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"}
    branches = []
    for intent, names in (
        ("faq", ["fact_quotes"]),
        ("conversation", ["answer"]),
        ("booking", (["service_id"] if matched_services else []) + spoken_fields),
        ("callback", ["message"] + [field for field in spoken_fields if field in ("name", "phone")]),
        ("cancel_draft", []),
        ("handoff", []),
        ("unknown", ["answer"]),
    ):
        properties = {"intent": {"type": "string", "const": intent}}
        properties.update({name: copy.deepcopy(fields[name]) for name in names})
        if "answer" in properties:
            properties["answer"] = {"type": "string", "maxLength": 400}
        if categories:
            properties["category"] = {"type": "string", "enum": [*categories, "unknown"]}
        required = ["intent"] + (["fact_quotes"] if intent == "faq" else ["answer"] if intent == "conversation" else [])
        if intent in ("booking", "callback"):
            required.extend(field for field in names if field in required_spoken)
        if intent == "booking" and matched_services:
            required.append("service_id")
        branches.append({"type": "object", "properties": properties, "required": required, "additionalProperties": False})
    return {"oneOf": branches}


def guard_intent_output(output, schema):
    """Apply the compact field boundary even if an inference server ignores it."""
    if not isinstance(output, dict):
        raise ValueError("Intent output must be an object")
    branch = next(
        (item for item in schema["oneOf"] if item["properties"]["intent"]["const"] == output.get("intent")),
        None,
    )
    if branch is None or any(field not in output for field in branch["required"]):
        raise ValueError("Intent output does not match a required branch")
    result = {}
    for name, field in branch["properties"].items():
        if name not in output:
            continue
        value = output[name]
        if "enum" in field and value not in field["enum"]:
            raise ValueError("Intent output does not match an allowed value")
        if "pattern" in field and (not isinstance(value, str) or not re.fullmatch(field["pattern"], value)):
            raise ValueError("Intent output does not match the field format")
        if "maxLength" in field and (not isinstance(value, str) or len(value) > field["maxLength"]):
            raise ValueError("Intent output exceeds the field limit")
        result[name] = value
    return result
