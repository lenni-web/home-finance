from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction

from .models import Transaction


def transfer_candidates(item, *, days=3):
    """Return likely opposite entries on another owned account, best match first."""
    if not item.reviewed or not item.amount:
        return []
    start = item.booking_date - timedelta(days=days)
    end = item.booking_date + timedelta(days=days)
    amount = Decimal(item.amount)
    candidates = (
        Transaction.objects.filter(
            reviewed=True,
            amount=-amount,
            currency=item.currency,
            booking_date__range=(start, end),
            transfer_counterpart__isnull=True,
        )
        .exclude(pk=item.pk)
        .exclude(statement_import__account_id=item.statement_import.account_id)
        .select_related("statement_import__account")
    )
    return sorted(
        candidates,
        key=lambda candidate: (abs((candidate.booking_date - item.booking_date).days), candidate.pk),
    )


def best_transfer_candidate(item):
    candidates = transfer_candidates(item)
    return candidates[0] if candidates else None


@db_transaction.atomic
def link_transfer_pair(first, second):
    if first.pk == second.pk:
        raise ValueError("Eine Buchung kann nicht mit sich selbst verknüpft werden.")
    if first.statement_import.account_id == second.statement_import.account_id:
        raise ValueError("Umbuchungen müssen zwei unterschiedliche Konten betreffen.")
    if first.currency != second.currency or Decimal(first.amount) != -Decimal(second.amount):
        raise ValueError("Die Gegenbuchung muss denselben Betrag mit umgekehrtem Vorzeichen haben.")
    unlink_transfer(first)
    unlink_transfer(second)
    first.is_internal_transfer = True
    first.transfer_counterpart = second
    second.is_internal_transfer = True
    second.transfer_counterpart = first
    first.save(update_fields=["is_internal_transfer", "transfer_counterpart", "updated_at"])
    second.save(update_fields=["is_internal_transfer", "transfer_counterpart", "updated_at"])


@db_transaction.atomic
def unlink_transfer(item, *, keep_marked=False):
    counterpart = item.transfer_counterpart
    item.transfer_counterpart = None
    item.is_internal_transfer = keep_marked
    item.save(update_fields=["is_internal_transfer", "transfer_counterpart", "updated_at"])
    if counterpart and counterpart.transfer_counterpart_id == item.pk:
        counterpart.transfer_counterpart = None
        counterpart.is_internal_transfer = keep_marked
        counterpart.save(
            update_fields=["is_internal_transfer", "transfer_counterpart", "updated_at"]
        )
