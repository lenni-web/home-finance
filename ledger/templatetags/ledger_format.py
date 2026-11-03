from decimal import ROUND_HALF_UP, Decimal

from django import template
from django.utils.formats import number_format

register = template.Library()


@register.filter
def money(value):
    """Format amounts with two decimals and locale-specific thousands separators."""
    if value is None or value == "":
        return ""
    return number_format(value, decimal_pos=2, use_l10n=True, force_grouping=True)


@register.simple_tag(takes_context=True)
def amount(context, value, base=None):
    """Betrag in Euro; im Präsentationsmodus nur als Prozentwert der Bezugsgröße.

    Die Bezugsgröße kommt aus ``base`` oder, sonst, aus dem Seitenkontext
    (``presentation_base``). Fehlt sie, erscheint ``•••`` statt des Betrags.
    """
    if value is None or value == "":
        return ""
    if not context.get("presentation_mode"):
        return f"{money(value)} €"
    base = base if base is not None else context.get("presentation_base")
    if not base:
        return "•••"
    # number_format schneidet Nachkommastellen ab; deshalb vorher kaufmännisch runden.
    percent = (Decimal(value) / Decimal(base) * 100).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return f"{number_format(percent, decimal_pos=1, use_l10n=True, force_grouping=True)} %"
