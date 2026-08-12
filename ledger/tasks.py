from celery import shared_task

from .document_processing import process_document
from .models import Document, EmailImportConfig, StatementImport
from .statement_processing import process_statement_import


@shared_task(name="ledger.process_document")
def process_document_task(document_id):
    document = Document.objects.get(pk=document_id)
    process_document(document)
    from .document_matching import auto_match_document
    auto_match_document(document)
    return document_id


@shared_task(name="ledger.process_statement")
def process_statement_task(statement_id):
    statement = StatementImport.objects.select_related("document").get(pk=statement_id)
    return process_statement_import(statement)


@shared_task(name="ledger.poll_email_import")
def poll_email_import_task(config_id=None, force=False):
    from .email_import import poll_mailbox

    queryset = EmailImportConfig.objects.filter(enabled=True)
    if config_id is not None:
        queryset = EmailImportConfig.objects.filter(pk=config_id)
    return sum(poll_mailbox(config, force=force) for config in queryset)
