from django import template

from apps.core.money import format_money

register = template.Library()


@register.filter
def money(amount_minor, currency="NGN"):
    if amount_minor is None:
        return "-"
    return format_money(amount_minor, currency)


_STATUS_TONE = {
    "trial": "tone-info",
    "active": "tone-good",
    "past_due": "tone-warn",
    "grace": "tone-warn",
    "restricted": "tone-bad",
    "suspended": "tone-bad",
    "cancelled": "tone-muted",
}


@register.filter
def status_tone(status):
    return _STATUS_TONE.get(status, "tone-muted")
