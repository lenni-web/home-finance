import hashlib
from pathlib import Path

from django.contrib import messages
from django.db import transaction
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404, redirect, render

from .forms import AccountForm, DocumentUploadForm, TransactionReviewFormSet
from .importers import INGStatementParser
from .models import Document, StatementImport, Transaction


def dashboard(request):
    months = (
        Document.objects.exclude(document_date=None)
        .annotate(month=TruncMonth("document_date"))
        .values("month")
        .order_by("-month")
        .distinct()
    )
    return render(request, "ledger/dashboard.html", {
        "documents": Document.objects.select_related("category").prefetch_related("tags", "people")[:20],
        "transactions": Transaction.objects.select_related("category")[:10],
        "statement_imports": StatementImport.objects.select_related("document", "account")[:10],
        "months": months,
        "account_form": AccountForm(),
        "upload_form": DocumentUploadForm(),
    })


def add_account(request):
    if request.method != "POST":
        return redirect("dashboard")
    form = AccountForm(request.POST)
    if form.is_valid():
        account = form.save()
        messages.success(request, f"Konto „{account.name}“ wurde angelegt und kann ausgewählt werden.")
    else:
        details = " ".join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f"Das Konto konnte nicht angelegt werden: {details}")
    return redirect("dashboard")


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
                status=StatementImport.Status.PROCESSING,
            )
            try:
                parsed = INGStatementParser().parse_pdf(Path(document.file.path))
                if not parsed:
                    raise ValueError("Im PDF wurden keine Buchungen erkannt.")
                with transaction.atomic():
                    for index, item in enumerate(parsed, start=1):
                        fingerprint_source = (
                            f"{document.sha256}:{item.source_page}:{index}:"
                            f"{item.booking_date}:{item.amount}:{item.counterparty}"
                        )
                        Transaction.objects.create(
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
                statement.status = StatementImport.Status.REVIEW
                statement.save(update_fields=["status", "updated_at"])
                messages.success(request, f"{len(parsed)} Buchungen erkannt. Bitte jetzt prüfen.")
                return redirect("statement_review", pk=statement.pk)
            except Exception as exc:
                statement.status = StatementImport.Status.FAILED
                statement.error_message = str(exc)
                statement.save(update_fields=["status", "error_message", "updated_at"])
                messages.error(request, "Der Kontoauszug konnte nicht verarbeitet werden.")
                return redirect("dashboard")
        messages.success(request, "Dokument wurde ins Archiv aufgenommen.")
    else:
        details = " ".join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f"Das Dokument konnte nicht gespeichert werden: {details}")
    return redirect("dashboard")


def statement_review(request, pk):
    statement = get_object_or_404(
        StatementImport.objects.select_related("document", "account"), pk=pk
    )
    queryset = statement.transactions.select_related("category").order_by("source_page", "id")
    if request.method == "POST" and statement.status == StatementImport.Status.REVIEW:
        formset = TransactionReviewFormSet(request.POST, queryset=queryset, prefix="transactions")
        if formset.is_valid():
            transactions = formset.save(commit=False)
            confirm = request.POST.get("action") == "confirm"
            for item in transactions:
                item.save()
            formset.save_m2m()
            if confirm:
                queryset.update(reviewed=True)
                statement.status = StatementImport.Status.IMPORTED
                statement.save(update_fields=["status", "updated_at"])
                messages.success(request, f"{queryset.count()} Buchungen wurden übernommen.")
                return redirect("dashboard")
            messages.success(request, "Korrekturen wurden gespeichert.")
            return redirect("statement_review", pk=statement.pk)
    else:
        formset = TransactionReviewFormSet(queryset=queryset, prefix="transactions")
    return render(request, "ledger/statement_review.html", {
        "statement": statement,
        "formset": formset,
        "transaction_count": queryset.count(),
    })
