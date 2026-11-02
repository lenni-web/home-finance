"""Gemeinsame Zeitraum- und Summenlogik für Übersicht, Auswertungen und Budgets."""

from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Transaction

ZERO = Value(Decimal("0.00"), output_field=DecimalField())


def latest_booking_date():
    """Datum der jüngsten bestätigten Buchung, sonst heute."""
    latest = (
        Transaction.objects.filter(reviewed=True).order_by("-booking_date")
        .values_list("booking_date", flat=True).first()
    )
    return latest or date.today()


def parse_month(value, fallback):
    """``YYYY-MM`` als Monatserster; bei ungültiger Eingabe der Monat von ``fallback``."""
    try:
        year, month = map(int, value.split("-"))
        return date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        return fallback.replace(day=1)


def month_end(month_start):
    return month_start.replace(day=monthrange(month_start.year, month_start.month)[1])


def previous_month(month_start):
    return (month_start.replace(day=1) - date.resolution).replace(day=1)


def financial_transactions(start, end):
    """Bestätigte Buchungen ohne interne Umbuchungen im Zeitraum."""
    return Transaction.objects.filter(
        reviewed=True, is_internal_transfer=False, booking_date__range=(start, end)
    )


def income_expense_totals(queryset):
    return queryset.aggregate(
        income=Coalesce(Sum("amount", filter=Q(amount__gt=0)), ZERO),
        expenses=Coalesce(Sum("amount", filter=Q(amount__lt=0)), ZERO),
        balance=Coalesce(Sum("amount"), ZERO),
    )
