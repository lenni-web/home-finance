import hashlib
from pathlib import Path

from django.db import transaction

from .importers import INGStatementParser
from .models import Document, StatementImport, Transaction
from .rules import apply_categorization_rules
from .statement_reconciliation import store_reconciliation


def process_statement_import(statement):
    if statement.status in {StatementImport.Status.REVIEW, StatementImport.Status.IMPORTED}:
        return statement.transactions.count()
    document = statement.document
    statement.status = StatementImport.Status.PROCESSING
    statement.error_message = ""
    statement.save(update_fields=["status", "error_message", "updated_at"])
    document.processing_status = Document.ProcessingStatus.PROCESSING
    document.processing_error = ""
    document.save(update_fields=["processing_status", "processing_error", "updated_at"])
    try:
        parsed_statement = INGStatementParser().parse_statement_pdf(Path(document.file.path))
        parsed = parsed_statement.transactions
        if not parsed:
            raise ValueError("Im PDF wurden keine Buchungen erkannt.")
        with transaction.atomic():
            statement.transactions.filter(reviewed=False).delete()
            for index, item in enumerate(parsed, start=1):
                fingerprint_source = (
                    f"{document.sha256}:{item.source_page}:{index}:"
                    f"{item.booking_date}:{item.amount}:{item.counterparty}"
                )
                created = Transaction.objects.create(
                    statement_import=statement,
                    booking_date=item.booking_date,
                    value_date=item.value_date,
                    booking_type=item.booking_type,
                    counterparty=item.counterparty,
                    description=item.description,
                    amount=item.amount,
                    currency=item.currency,
                    source_page=item.source_page,
                    source_fingerprint=hashlib.sha256(
                        fingerprint_source.encode("utf-8")
                    ).hexdigest(),
                )
                apply_categorization_rules(created)
        statement.status = StatementImport.Status.REVIEW
        statement.save(update_fields=["status", "updated_at"])
        store_reconciliation(statement, parsed_statement)
        document.processing_status = Document.ProcessingStatus.REVIEW
        document.save(update_fields=["processing_status", "updated_at"])
        return len(parsed)
    except Exception as exc:
        statement.status = StatementImport.Status.FAILED
        statement.error_message = str(exc)
        statement.save(update_fields=["status", "error_message", "updated_at"])
        document.processing_status = Document.ProcessingStatus.FAILED
        document.processing_error = str(exc)
        document.save(update_fields=["processing_status", "processing_error", "updated_at"])
        raise
