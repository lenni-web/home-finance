from datetime import timedelta

from django.db.models import Q

from .models import Document, Transaction


def document_transaction_candidates(document):
    queryset = Transaction.objects.filter(reviewed=True).select_related("statement_import__account")
    if document.document_date:
        queryset = queryset.filter(
            booking_date__range=(
                document.document_date - timedelta(days=7),
                document.document_date + timedelta(days=7),
            )
        )
    if document.total_amount is not None:
        amount = abs(document.total_amount)
        exact = queryset.filter(Q(amount=amount) | Q(amount=-amount))
        if exact.exists():
            queryset = exact
    return queryset.order_by("-booking_date", "-id")


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
        if document_transaction_candidates(document).exists():
            if document.processing_status != Document.ProcessingStatus.REVIEW:
                document.processing_status = Document.ProcessingStatus.REVIEW
                document.save(update_fields=["processing_status", "updated_at"])
            matched_documents.append(document)
    return matched_documents
