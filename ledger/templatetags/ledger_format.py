from django import template
from django.utils.formats import number_format

register = template.Library()


@register.filter
def money(value):
    """Format amounts with two decimals and locale-specific thousands separators."""
    if value is None or value == "":
        return ""
    return number_format(value, decimal_pos=2, use_l10n=True, force_grouping=True)
