import mimetypes
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Count, F, Q
from django.db.models.functions import TruncMonth
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from ..document_matching import (
    document_transaction_candidates, scored_document_transaction_candidates,
)
from ..forms import (
    DocumentArchiveFilterForm, DocumentReviewForm, DocumentTransactionLinkForm,
    DocumentUploadForm,
)
from ..importers import INGStatementParser
from ..models import Document, StatementImport
from ..tasks import process_document_task, process_statement_task


@login_required
def upload_document(request):
    if request.method != "POST":
        return redirect("dashboard")
    form = DocumentUploadForm(request.POST, request.FILES)
    if form.is_valid():
        document = form.save()
        if document.kind == Document.Kind.BANK_STATEMENT:
            statement = StatementImport.objects.create(
                document=document,
                account=form.cleaned_data["account"],
                parser_name=INGStatementParser.name,
                parser_version=INGStatementParser.version,
                status=StatementImport.Status.UPLOADED,
            )
            process_statement_task.delay(statement.pk)
            messages.success(request, "Kontoauszug wurde zur Hintergrundverarbeitung vorgemerkt.")
            return redirect("dashboard")
        process_document_task.delay(document.pk)
        messages.success(request, "Dokument wurde zur Hintergrundverarbeitung vorgemerkt.")
        return redirect("dashboard")
    else:
        details = " ".join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f"Das Dokument konnte nicht gespeichert werden: {details}")
    return redirect("dashboard")


@login_required
def document_review(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("category").prefetch_related("tags", "people", "transactions"),
        pk=pk,
    )
    if document.kind == Document.Kind.BANK_STATEMENT:
        statement = StatementImport.objects.filter(document=document).select_related(
            "account"
        ).first()
        return render(request, "ledger/document_review.html", {
            "document": document,
            "statement": statement,
            "is_bank_statement": True,
        })
    scored_candidates = scored_document_transaction_candidates(document)
    candidates = document_transaction_candidates(document)
    detail_map = {
        candidate.transaction.pk: (
            f"{candidate.confidence} % passend: {', '.join(candidate.reasons)}"
        )
        for candidate in scored_candidates
    }
    for candidate in candidates:
        candidate.match_details = detail_map.get(candidate.pk, "")
    if request.method == "POST":
        if request.POST.get("action") == "retry":
            if document.title == document.merchant:
                document.title = ""
            document.merchant = ""
            document.total_amount = None
            document.invoice_number = ""
            document.extraction_confidence = {}
            document.save(update_fields=[
                "title", "merchant", "total_amount", "invoice_number", "extraction_confidence",
                "updated_at",
            ])
            process_document_task.delay(document.pk)
            messages.success(request, "Erneute Analyse wurde vorgemerkt.")
            return redirect("document_review", pk=document.pk)
        review_form = DocumentReviewForm(request.POST, instance=document, prefix="document")
        link_form = DocumentTransactionLinkForm(
            request.POST, queryset=candidates, candidate_details=detail_map, prefix="links"
        )
        if review_form.is_valid() and link_form.is_valid():
            document = review_form.save()
            document.transactions.set(link_form.cleaned_data["transactions"])
            document.processing_status = Document.ProcessingStatus.PROCESSED
            document.auto_matched_transaction = None
            document.auto_match_confidence = None
            document.auto_match_reasons = []
            document.auto_matched_at = None
            document.save(update_fields=[
                "processing_status", "auto_matched_transaction", "auto_match_confidence",
                "auto_match_reasons", "auto_matched_at", "updated_at",
            ])
            messages.success(request, "Dokument und Zuordnung wurden gespeichert.")
            return redirect("document_archive")
    else:
        review_form = DocumentReviewForm(instance=document, prefix="document")
        link_form = DocumentTransactionLinkForm(
            queryset=candidates, candidate_details=detail_map,
            prefix="links",
            initial={"transactions": document.transactions.all()},
        )
    return render(request, "ledger/document_review.html", {
        "document": document,
        "review_form": review_form,
        "link_form": link_form,
        "extraction_confidence": document.extraction_confidence,
    })


@login_required
@require_POST
def retry_failed_document(request, pk):
    document = get_object_or_404(
        Document, pk=pk, processing_status=Document.ProcessingStatus.FAILED
    )
    statement = None
    if document.kind == Document.Kind.BANK_STATEMENT:
        statement = get_object_or_404(
            StatementImport, document=document, status=StatementImport.Status.FAILED
        )
    document.processing_status = Document.ProcessingStatus.PENDING
    document.processing_error = ""
    document.save(update_fields=["processing_status", "processing_error", "updated_at"])

    if statement:
        statement.status = StatementImport.Status.UPLOADED
        statement.error_message = ""
        statement.save(update_fields=["status", "error_message", "updated_at"])
        process_statement_task.delay(statement.pk)
    else:
        process_document_task.delay(document.pk)

    messages.success(request, "Die erneute Verarbeitung wurde gestartet.")
    return redirect("document_review", pk=document.pk)


@login_required
@require_POST
def delete_failed_document(request, pk):
    document = get_object_or_404(
        Document, pk=pk, processing_status=Document.ProcessingStatus.FAILED
    )
    if document.transactions.exists():
        messages.error(
            request,
            "Das Dokument ist bereits mit Buchungen verknüpft und kann nicht gelöscht werden.",
        )
        return redirect("document_review", pk=document.pk)

    file_name = document.file.name
    storage = document.file.storage
    with db_transaction.atomic():
        statement = StatementImport.objects.filter(document=document).first()
        if statement:
            if statement.status != StatementImport.Status.FAILED or statement.transactions.exists():
                messages.error(
                    request,
                    "Der Kontoauszug enthält bereits verarbeitete Daten und kann nicht gelöscht werden.",
                )
                return redirect("document_review", pk=document.pk)
            statement.delete()
        document.delete()
        if file_name:
            db_transaction.on_commit(lambda: storage.delete(file_name))

    messages.success(request, "Das fehlgeschlagene Dokument wurde vollständig gelöscht.")
    return redirect("document_archive")


@login_required
def automatic_matches(request):
    matches = Document.objects.filter(
        auto_matched_transaction__isnull=False
    ).select_related(
        "auto_matched_transaction__statement_import__account"
    ).order_by("-auto_matched_at", "-id")
    return render(request, "ledger/automatic_matches.html", {"matches": matches})


@login_required
@require_POST
def revoke_automatic_match(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("auto_matched_transaction"),
        pk=pk,
        auto_matched_transaction__isnull=False,
    )
    transaction = document.auto_matched_transaction
    document.transactions.remove(transaction)
    document.auto_matched_transaction = None
    document.auto_match_confidence = None
    document.auto_match_reasons = []
    document.auto_matched_at = None
    document.processing_status = Document.ProcessingStatus.REVIEW
    document.save(update_fields=[
        "auto_matched_transaction", "auto_match_confidence", "auto_match_reasons",
        "auto_matched_at", "processing_status", "updated_at",
    ])
    messages.success(request, f"Automatische Zuordnung für „{document}“ wurde widerrufen.")
    return redirect("automatic_matches")


@login_required
def document_archive(request):
    queryset = Document.objects.select_related("category").prefetch_related(
        "tags", "people", "transactions"
    ).defer("extracted_text").order_by("-document_date", "-created_at")
    filters = DocumentArchiveFilterForm(request.GET)
    if filters.is_valid():
        values = filters.cleaned_data
        if values.get("month"):
            try:
                year, month = map(int, values["month"].split("-"))
                queryset = queryset.filter(document_date__year=year, document_date__month=month)
            except (TypeError, ValueError):
                filters.add_error("month", "Bitte einen gültigen Monat auswählen.")
        if values.get("date_from"):
            queryset = queryset.filter(document_date__gte=values["date_from"])
        if values.get("date_to"):
            queryset = queryset.filter(document_date__lte=values["date_to"])
        if values.get("category"):
            queryset = queryset.filter(category=values["category"])
        if values.get("link_status") == "linked":
            queryset = queryset.filter(transactions__isnull=False)
        elif values.get("link_status") == "unlinked":
            queryset = queryset.filter(transactions__isnull=True)
        if values.get("kind"):
            queryset = queryset.filter(kind=values["kind"])
        if values.get("tag"):
            queryset = queryset.filter(tags=values["tag"])
        if values.get("person"):
            queryset = queryset.filter(people=values["person"])
        if values.get("q"):
            queryset = queryset.filter(
                Q(title__icontains=values["q"])
                | Q(merchant__icontains=values["q"])
                | Q(invoice_number__icontains=values["q"])
                | Q(original_filename__icontains=values["q"])
                | Q(extracted_text__icontains=values["q"])
            )
    # Gruppierung und Seiteneinteilung erfolgen in der Datenbank, damit weder alle Dokumente
    # noch deren OCR-Texte in den Speicher geladen werden.
    month_buckets = (
        queryset.order_by().annotate(bucket=TruncMonth("document_date"))
        .values("bucket").annotate(total=Count("pk", distinct=True))
        .order_by(F("bucket").desc(nulls_last=True))
    )
    archive_groups = []
    for bucket in month_buckets:
        month_start = bucket["bucket"]
        if month_start is None:
            month_key, month_label = "undated", "Ohne Datum"
            documents = queryset.filter(document_date__isnull=True)
        else:
            month_key, month_label = month_start.strftime("%Y_%m"), month_start.strftime("%m/%Y")
            next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            documents = queryset.filter(
                document_date__gte=month_start, document_date__lt=next_month
            )
        group = {"key": month_key, "label": month_label}
        paginator = Paginator(documents.distinct(), 10)
        paginator.count = bucket["total"]  # bereits aggregiert, spart eine COUNT-Abfrage
        parameter = f"page_{group['key']}"
        page = paginator.get_page(request.GET.get(parameter, 1))
        pagination_links = []
        if paginator.num_pages > 1:
            for page_number in paginator.get_elided_page_range(
                page.number, on_each_side=2, on_ends=1
            ):
                if page_number == paginator.ELLIPSIS:
                    pagination_links.append({"ellipsis": True})
                    continue
                params = request.GET.copy()
                params[parameter] = page_number
                pagination_links.append({
                    "number": page_number,
                    "current": page_number == page.number,
                    "url": f"?{params.urlencode()}",
                })
        previous_url = next_url = ""
        if page.has_previous():
            params = request.GET.copy()
            params[parameter] = page.previous_page_number()
            previous_url = f"?{params.urlencode()}"
        if page.has_next():
            params = request.GET.copy()
            params[parameter] = page.next_page_number()
            next_url = f"?{params.urlencode()}"
        group.update({
            "page": page,
            "total": paginator.count,
            "pagination_links": pagination_links,
            "previous_url": previous_url,
            "next_url": next_url,
        })
        archive_groups.append(group)
    return render(request, "ledger/document_archive.html", {
        "archive_groups": archive_groups,
        "filters": filters,
    })


@login_required
@xframe_options_sameorigin
def document_download(request, pk):
    document = get_object_or_404(Document, pk=pk)
    content_type = mimetypes.guess_type(document.original_filename)[0] or "application/octet-stream"
    return FileResponse(
        document.file.open("rb"),
        as_attachment=request.GET.get("download") == "1",
        filename=document.original_filename,
        content_type=content_type,
    )
