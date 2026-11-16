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
    "restricted": "tone-serious",
    "suspended": "tone-critical",
    "cancelled": "tone-muted",
    "open": "tone-info",
    "frozen": "tone-good",
    "needs_review": "tone-warn",
    "corrected": "tone-muted",
}


@register.filter
def status_tone(status):
    return _STATUS_TONE.get(status, "tone-muted")


@register.simple_tag(takes_context=True)
def nav_active(context, *url_names):
    """"active" when the current view's URL name is one of url_names - lets the
    nav bar show which section an operator is in, including a detail page
    (e.g. a school's discount page) nested under a top-level tab."""
    match = context.get("request") and context["request"].resolver_match
    return "active" if match and match.url_name in url_names else ""
