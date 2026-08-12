from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .models import Document, Transaction
from .text_normalization import comparison_words, words_match


@dataclass(frozen=True)
class DocumentMatchCandidate:
    transaction: Transaction
    confidence: int
    reasons: tuple[str, ...]


def _words(value):
    return comparison_words(value)


def scored_document_transaction_candidates(document):
    queryset = Transaction.objects.filter(reviewed=True).select_related("statement_import__account")
    if document.document_date:
        queryset = queryset.filter(
            booking_date__range=(
                document.document_date - timedelta(days=7),
                document.document_date + timedelta(days=7),
            )
        )
    if document.total_amount is not None:
        amount = abs(Decimal(document.total_amount))
        queryset = queryset.filter(Q(amount=amount) | Q(amount=-amount))

    merchant_words = _words(document.merchant or document.title)
    candidates = []
    for transaction in queryset.order_by("-booking_date", "-id"):
        score = 0
        reasons = []
        if (
            document.total_amount is not None
            and abs(transaction.amount) == abs(Decimal(document.total_amount))
        ):
            score += 55
            reasons.append("gleicher Betrag")
        if document.document_date:
            difference = abs((transaction.booking_date - document.document_date).days)
            if difference == 0:
                score += 30
                reasons.append("gleiches Datum")
            elif difference <= 3:
                score += 20
                reasons.append(f"Datum ±{difference} Tage")
            elif difference <= 7:
                score += 10
                reasons.append(f"Datum ±{difference} Tage")
        transaction_words = _words(f"{transaction.counterparty} {transaction.description}")
        overlap_count = sum(
            1 for merchant_word in merchant_words
            if any(words_match(merchant_word, transaction_word) for transaction_word in transaction_words)
        )
        if overlap_count:
            score += min(15, 5 * overlap_count)
            reasons.append("passender Händlertext")
        candidates.append(DocumentMatchCandidate(transaction, min(score, 100), tuple(reasons)))
    return sorted(candidates, key=lambda item: (-item.confidence, -item.transaction.booking_date.toordinal()))


def document_transaction_candidates(document):
    ids = [item.transaction.pk for item in scored_document_transaction_candidates(document)]
    return Transaction.objects.filter(pk__in=ids).select_related("statement_import__account").order_by(
        "-booking_date", "-id"
    )


def auto_match_document(document):
    if document.kind == Document.Kind.BANK_STATEMENT or document.transactions.exists():
        return None
    candidates = scored_document_transaction_candidates(document)
    if not candidates or candidates[0].confidence < 85:
        return None
    if len(candidates) > 1 and candidates[0].confidence - candidates[1].confidence < 15:
        return None
    document.transactions.add(candidates[0].transaction)
    document.processing_status = Document.ProcessingStatus.PROCESSED
    document.auto_matched_transaction = candidates[0].transaction
    document.auto_match_confidence = candidates[0].confidence
    document.auto_match_reasons = list(candidates[0].reasons)
    document.auto_matched_at = timezone.now()
    document.save(update_fields=[
        "processing_status", "auto_matched_transaction", "auto_match_confidence",
        "auto_match_reasons", "auto_matched_at", "updated_at",
    ])
    return candidates[0]


def refresh_unmatched_document_reviews():
    documents = (
        Document.objects.exclude(kind=Document.Kind.BANK_STATEMENT)
        .filter(document_date__isnull=False, total_amount__isnull=False)
        .prefetch_related("transactions")
    )
    matched_documents = []
    for document in documents:
        if document.transactions.exists():
            continue
        automatic_match = auto_match_document(document)
        if automatic_match:
            matched_documents.append(document)
            continue
        if scored_document_transaction_candidates(document):
            if document.processing_status != Document.ProcessingStatus.REVIEW:
                document.processing_status = Document.ProcessingStatus.REVIEW
                document.save(update_fields=["processing_status", "updated_at"])
            matched_documents.append(document)
    return matched_documents
