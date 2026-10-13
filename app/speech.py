"""Russian spoken forms, independent of the selected speech backend."""

import re
import textwrap

from num2words import num2words

MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
DIGITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]


def plural(number, forms):
    number = abs(int(number))
    if 11 <= number % 100 <= 14:
        return forms[2]
    if number % 10 == 1:
        return forms[0]
    if 2 <= number % 10 <= 4:
        return forms[1]
    return forms[2]


def normalize(text):
    def date(match):
        day, month, year = map(int, match.groups())
        if not 1 <= day <= 31 or not 1 <= month <= 12:
            return match[0]
        return f"{num2words(day, to='ordinal', gender='neuter', case='genitive', lang='ru')} {MONTHS[month - 1]} {num2words(year, to='ordinal', gender='neuter', case='genitive', lang='ru')} года"

    def money(match):
        value = int(match[1])
        return (
            num2words(value, lang="ru")
            + " "
            + plural(value, ["белорусский рубль", "белорусских рубля", "белорусских рублей"])
        )

    def clock(match):
        hour, minute = map(int, match.groups())
        if hour > 23 or minute > 59:
            return match[0]
        result = num2words(hour, lang="ru") + " " + plural(hour, ["час", "часа", "часов"])
        if minute:
            result += (
                " "
                + num2words(minute, lang="ru", gender="feminine")
                + " "
                + plural(minute, ["минута", "минуты", "минут"])
            )
        return result

    def phone(match):
        number = match[1]
        groups = (
            [number[:3], number[3:5], number[5:8], number[8:10], number[10:]]
            if number.startswith("375") and len(number) == 12
            else [number[i : i + 3] for i in range(0, len(number), 3)]
        )
        return "плюс " + ", ".join(" ".join(DIGITS[int(d)] for d in group) for group in groups if group)

    text = re.sub(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", date, text)
    text = re.sub(r"\b(\d{1,6})\s*BYN\b", money, text, flags=re.I)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, text)
    text = re.sub(r"\+(\d{8,15})\b", phone, text)
    text = text.replace("CRM", "си эр эм").replace("BYN", "белорусских рублей")
    return re.sub(r"[*#`_]", "", text)[:6000]


def segments(text, max_chars=350):
    text = normalize(text)
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        yield from textwrap.wrap(
            sentence, width=max_chars, break_long_words=True, break_on_hyphens=False, replace_whitespace=True
        )
