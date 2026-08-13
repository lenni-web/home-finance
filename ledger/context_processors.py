from django.db.models import Q

from .models import Document, StatementImport, Transaction


def navigation_status(request):
    if not request.user.is_authenticated:
        return {}
    document_tasks = Document.objects.filter(
        processing_status__in=[
            Document.ProcessingStatus.REVIEW,
            Document.ProcessingStatus.FAILED,
        ]
    ).count()
    statement_tasks = StatementImport.objects.filter(
        Q(status=StatementImport.Status.REVIEW)
        | Q(reconciliation_status=StatementImport.ReconciliationStatus.MISMATCH)
    ).distinct().count()
    uncategorized = Transaction.objects.filter(
        reviewed=True, category=None, is_internal_transfer=False
    ).count()
    return {"navigation_task_count": document_tasks + statement_tasks + uncategorized}
