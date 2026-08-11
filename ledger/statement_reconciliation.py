from decimal import Decimal
from pathlib import Path

from .importers import INGStatementParser
from .models import StatementImport


def store_reconciliation(statement, parsed_statement):
    statement.opening_balance = parsed_statement.opening_balance
    statement.closing_balance = parsed_statement.closing_balance
    statement.transaction_total = parsed_statement.transaction_total
    statement.reconciliation_difference = parsed_statement.reconciliation_difference
    if parsed_statement.reconciliation_difference is None:
        statement.reconciliation_status = StatementImport.ReconciliationStatus.UNAVAILABLE
    elif parsed_statement.reconciliation_difference == Decimal("0.00"):
        statement.reconciliation_status = StatementImport.ReconciliationStatus.BALANCED
    else:
        statement.reconciliation_status = StatementImport.ReconciliationStatus.MISMATCH
    statement.save(update_fields=[
        "opening_balance", "closing_balance", "transaction_total",
        "reconciliation_difference", "reconciliation_status", "updated_at",
    ])
    return statement


def recalculate_statement(statement):
    parsed_statement = INGStatementParser().parse_statement_pdf(
        Path(statement.document.file.path)
    )
    return store_reconciliation(statement, parsed_statement)
