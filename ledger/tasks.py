from celery import shared_task

from .document_processing import process_document
from .models import Document, StatementImport
from .statement_processing import process_statement_import


@shared_task(name="ledger.process_document")
def process_document_task(document_id):
    document = Document.objects.get(pk=document_id)
    process_document(document)
    return document_id


@shared_task(name="ledger.process_statement")
def process_statement_task(statement_id):
    statement = StatementImport.objects.select_related("document").get(pk=statement_id)
    return process_statement_import(statement)
