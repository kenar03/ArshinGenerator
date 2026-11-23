from datetime import date, timedelta


def expiry_before_anniversary(day: date, years: int) -> date:
    """Return the last calendar day before the specified anniversary."""
    if type(day) is not date or type(years) is not int or years < 1:
        raise ValueError("Для расчёта нужны дата поверки и МПИ в полных годах.")
    if day.year + years > date.max.year:
        raise ValueError("МПИ выводит дату окончания за допустимый диапазон дат.")
    try:
        anniversary = day.replace(year=day.year + years)
    except ValueError as exc:
        raise ValueError("В году окончания нет 29 февраля. По этому правилу срок однозначно не определяется.") from exc
    return anniversary - timedelta(days=1)
