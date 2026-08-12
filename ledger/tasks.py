from celery import shared_task

from .document_processing import process_document
from django.utils import timezone

from .models import Document, EmailImportConfig, ServiceHeartbeat, StatementImport


def _heartbeat(name, **details):
    ServiceHeartbeat.objects.update_or_create(
        name=name,
        defaults={"last_seen_at": timezone.now(), "details": details},
    )
from .statement_processing import process_statement_import


@shared_task(name="ledger.process_document")
def process_document_task(document_id):
    _heartbeat("worker", task="process_document")
    document = Document.objects.get(pk=document_id)
    process_document(document)
    from .document_matching import auto_match_document
    auto_match_document(document)
    return document_id


@shared_task(name="ledger.process_statement")
def process_statement_task(statement_id):
    _heartbeat("worker", task="process_statement")
    statement = StatementImport.objects.select_related("document").get(pk=statement_id)
    return process_statement_import(statement)


@shared_task(name="ledger.poll_email_import")
def poll_email_import_task(config_id=None, force=False):
    from .email_import import poll_mailbox

    _heartbeat("worker", task="poll_email_import")
    queryset = EmailImportConfig.objects.filter(enabled=True)
    if config_id is not None:
        queryset = EmailImportConfig.objects.filter(pk=config_id)
    try:
        result = sum(poll_mailbox(config, force=force) for config in queryset)
    except Exception as exc:
        _heartbeat("email_import", status="error", error=str(exc)[:500])
        raise
    _heartbeat("email_import", status="ok", imported=result)
    return result
