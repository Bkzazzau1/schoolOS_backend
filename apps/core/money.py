"""Writing money the way people read it. Amounts are whole minor units (kobo) everywhere in SchoolOS."""


def format_money(amount_minor: int, currency: str = "NGN") -> str:
    """Kobo as naira: 5000000 -> ₦50,000 and 150050 -> ₦1,500.50."""
    symbol = "₦" if currency == "NGN" else f"{currency} "
    negative = amount_minor < 0
    whole, kobo = divmod(abs(amount_minor), 100)
    text = f"{symbol}{whole:,}" if kobo == 0 else f"{symbol}{whole:,}.{kobo:02d}"
    return f"-{text}" if negative else text
