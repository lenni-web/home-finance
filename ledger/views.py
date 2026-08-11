import hashlib
from pathlib import Path

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404, redirect, render

from .forms import (
    AccountForm, BulkCategorizationForm, CategorizationRuleForm, CategoryForm,
    DocumentUploadForm, PersonForm, TagForm, TransactionCategorizationFormSet,
    TransactionFilterForm, TransactionReviewFormSet,
)
from .importers import INGStatementParser
from .models import CategorizationRule, Category, Document, Person, StatementImport, Tag, Transaction
from .rules import apply_categorization_rules, matching_rules


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


def transaction_overview(request):
    queryset = (
        Transaction.objects.filter(reviewed=True)
        .select_related("category", "statement_import__account")
        .prefetch_related("tags", "people")
        .order_by("-booking_date", "-id")
    )
    filters = TransactionFilterForm(request.GET)
    if filters.is_valid():
        values = filters.cleaned_data
        if values.get("month"):
            try:
                year, month = map(int, values["month"].split("-"))
                queryset = queryset.filter(booking_date__year=year, booking_date__month=month)
            except (TypeError, ValueError):
                filters.add_error("month", "Bitte einen gültigen Monat auswählen.")
        if values.get("account"):
            queryset = queryset.filter(statement_import__account=values["account"])
        if values.get("category"):
            queryset = queryset.filter(category=values["category"])
        if values.get("tag"):
            queryset = queryset.filter(tags=values["tag"])
        if values.get("person"):
            queryset = queryset.filter(people=values["person"])
        if values.get("uncategorized"):
            queryset = queryset.filter(category__isnull=True)
        if values.get("q"):
            queryset = queryset.filter(
                Q(counterparty__icontains=values["q"]) | Q(description__icontains=values["q"])
            )
    queryset = queryset.distinct()

    if request.method == "POST":
        formset = TransactionCategorizationFormSet(
            request.POST, queryset=queryset, prefix="transactions"
        )
        bulk_form = BulkCategorizationForm(request.POST, prefix="bulk")
        action = request.POST.get("action")
        if formset.is_valid() and bulk_form.is_valid():
            formset.save()
            if action == "bulk":
                selected_ids = request.POST.getlist("selected")
                selected = queryset.filter(pk__in=selected_ids)
                bulk = bulk_form.cleaned_data
                if not selected_ids:
                    messages.error(request, "Bitte mindestens eine Buchung auswählen.")
                else:
                    for item in selected:
                        if bulk.get("category"):
                            item.category = bulk["category"]
                            item.save(update_fields=["category", "updated_at"])
                        item.tags.add(*bulk["tags"])
                        item.people.add(*bulk["people"])
                    if bulk.get("create_rules"):
                        _create_rules_from_transactions(selected, bulk)
                    messages.success(request, f"{selected.count()} Buchungen wurden bearbeitet.")
                    return redirect(request.get_full_path())
            else:
                messages.success(request, "Zuordnungen wurden gespeichert.")
                return redirect(request.get_full_path())
    else:
        formset = TransactionCategorizationFormSet(queryset=queryset, prefix="transactions")
        bulk_form = BulkCategorizationForm(prefix="bulk")

    totals = queryset.aggregate(
        income=Sum("amount", filter=Q(amount__gt=0), default=0),
        expenses=Sum("amount", filter=Q(amount__lt=0), default=0),
        balance=Sum("amount", default=0),
    )
    for form in formset.forms:
        suggestions = matching_rules(form.instance)
        form.instance.rule_suggestion = suggestions[0] if suggestions else None
    return render(request, "ledger/transaction_overview.html", {
        "filters": filters,
        "formset": formset,
        "bulk_form": bulk_form,
        "totals": totals,
        "transaction_count": queryset.count(),
        "uncategorized_count": queryset.filter(category__isnull=True).count(),
    })


def _create_rules_from_transactions(transactions, bulk):
    for item in transactions:
        match_text = item.counterparty.strip()
        if not match_text:
            continue
        rule = CategorizationRule.objects.filter(match_text__iexact=match_text).first()
        if rule is None:
            rule = CategorizationRule.objects.create(
                name=f"{match_text} zuordnen"[:160],
                match_text=match_text[:255],
                category=bulk.get("category"),
                auto_apply=bulk.get("auto_apply", False),
            )
        elif bulk.get("category"):
            rule.category = bulk["category"]
            rule.auto_apply = bulk.get("auto_apply", False)
            rule.save(update_fields=["category", "auto_apply", "updated_at"])
        rule.tags.add(*bulk["tags"])
        rule.people.add(*bulk["people"])


def manage_classification(request):
    forms = {
        "category": CategoryForm(prefix="category"),
        "tag": TagForm(prefix="tag"),
        "person": PersonForm(prefix="person"),
        "rule": CategorizationRuleForm(prefix="rule"),
    }
    if request.method == "POST":
        kind = request.POST.get("kind")
        form_classes = {
            "category": CategoryForm,
            "tag": TagForm,
            "person": PersonForm,
            "rule": CategorizationRuleForm,
        }
        if kind in form_classes:
            form = form_classes[kind](request.POST, prefix=kind)
            forms[kind] = form
            if form.is_valid():
                created = form.save()
                messages.success(request, f"„{created}“ wurde angelegt.")
                return redirect("manage_classification")
    return render(request, "ledger/manage_classification.html", {
        "forms": forms,
        "categories": Category.objects.order_by("name"),
        "tags": Tag.objects.order_by("name"),
        "people": Person.objects.order_by("name"),
        "rules": CategorizationRule.objects.select_related("category").prefetch_related(
            "tags", "people"
        ),
    })


def toggle_classification(request, kind, pk):
    if request.method != "POST":
        return redirect("manage_classification")
    models = {"category": Category, "tag": Tag, "person": Person, "rule": CategorizationRule}
    model = models.get(kind)
    if model is None:
        return redirect("manage_classification")
    item = get_object_or_404(model, pk=pk)
    item.active = not item.active
    item.save(update_fields=["active", "updated_at"])
    messages.success(request, f"„{item}“ wurde {'aktiviert' if item.active else 'deaktiviert'}.")
    return redirect("manage_classification")
