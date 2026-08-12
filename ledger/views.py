import mimetypes
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db.models.functions import TruncMonth
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import (
    AccountForm, BulkCategorizationForm, CategorizationRuleForm, CategoryForm,
    DocumentArchiveFilterForm, DocumentReviewForm, DocumentTransactionLinkForm,
    DocumentUploadForm, EmailImportConfigForm, PersonForm, TagForm, TransactionCategorizationFormSet,
    TransactionFilterForm, TransactionReviewFormSet,
)
from .document_matching import document_transaction_candidates, refresh_unmatched_document_reviews
from .importers import INGStatementParser
from .models import (
    Account, CategorizationRule, Category, Document, EmailImportConfig, EmailImportMessage,
    Person, StatementImport, Tag, Transaction,
)
from .rules import (
    apply_rule_application_plan, build_rule_application_plan, categorization_suggestion,
)
from .tasks import poll_email_import_task, process_document_task, process_statement_task


@login_required
def dashboard(request):
    months = (
        Document.objects.exclude(document_date=None)
        .annotate(month=TruncMonth("document_date"))
        .values("month")
        .order_by("-month")
        .distinct()
    )
    latest_date = Transaction.objects.filter(reviewed=True).order_by("-booking_date").values_list(
        "booking_date", flat=True
    ).first()
    selected_month = request.GET.get("month")
    try:
        year, month = map(int, selected_month.split("-")) if selected_month else (
            (latest_date or date.today()).year, (latest_date or date.today()).month
        )
        month_start = date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        month_start = date((latest_date or date.today()).year, (latest_date or date.today()).month, 1)
    month_end = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
    if month_start.month == 1:
        previous_start = date(month_start.year - 1, 12, 1)
    else:
        previous_start = date(month_start.year, month_start.month - 1, 1)
    previous_end = month_start.fromordinal(month_start.toordinal() - 1)

    current = Transaction.objects.filter(
        reviewed=True, booking_date__range=(month_start, month_end)
    )
    previous = Transaction.objects.filter(
        reviewed=True, booking_date__range=(previous_start, previous_end)
    )
    zero = Value(Decimal("0.00"), output_field=DecimalField())
    totals = current.aggregate(
        income=Coalesce(Sum("amount", filter=Q(amount__gt=0)), zero),
        expenses=Coalesce(Sum("amount", filter=Q(amount__lt=0)), zero),
        balance=Coalesce(Sum("amount"), zero),
    )
    previous_expenses = abs(previous.aggregate(
        value=Coalesce(Sum("amount", filter=Q(amount__lt=0)), zero)
    )["value"])
    current_expenses = abs(totals["expenses"])
    expense_change = None
    if previous_expenses:
        expense_change = round((current_expenses - previous_expenses) / previous_expenses * 100)
    category_expenses = list(
        current.filter(amount__lt=0).values("category__name", "category__color")
        .annotate(total=Sum("amount")).order_by("total")
    )
    max_category = max((abs(item["total"]) for item in category_expenses), default=Decimal("0"))
    for item in category_expenses:
        item["amount"] = abs(item["total"])
        item["name"] = item["category__name"] or "Ohne Kategorie"
        item["color"] = item["category__color"] or "#94a3b8"
        item["percent"] = round(item["amount"] / current_expenses * 100) if current_expenses else 0
        item["bar_percent"] = round(item["amount"] / max_category * 100) if max_category else 0

    task_counts = _open_task_counts()
    return render(request, "ledger/dashboard.html", {
        "documents": Document.objects.select_related("category").prefetch_related("tags", "people")[:20],
        "transactions": Transaction.objects.select_related("category")[:10],
        "statement_imports": StatementImport.objects.select_related("document", "account")[:10],
        "months": months,
        "upload_form": DocumentUploadForm(),
        "selected_month": month_start.strftime("%Y-%m"),
        "selected_month_date": month_start,
        "totals": totals,
        "expenses_absolute": current_expenses,
        "expense_change": expense_change,
        "category_expenses": category_expenses,
        "uncategorized_count": current.filter(category__isnull=True).count(),
        "task_counts": task_counts,
        "open_task_total": sum(task_counts.values()),
    })


def _open_task_counts():
    return {
        "statements": StatementImport.objects.filter(status=StatementImport.Status.REVIEW).count(),
        "documents": Document.objects.filter(
            processing_status=Document.ProcessingStatus.REVIEW
        ).exclude(kind=Document.Kind.BANK_STATEMENT).count(),
        "failed": Document.objects.filter(processing_status=Document.ProcessingStatus.FAILED).count(),
        "uncategorized": Transaction.objects.filter(reviewed=True, category__isnull=True).count(),
        "mismatches": StatementImport.objects.filter(
            reconciliation_status=StatementImport.ReconciliationStatus.MISMATCH
        ).count(),
    }


@login_required
def open_tasks(request):
    return render(request, "ledger/open_tasks.html", {
        "counts": _open_task_counts(),
        "statements": StatementImport.objects.filter(status=StatementImport.Status.REVIEW)
            .select_related("account", "document"),
        "documents": Document.objects.filter(processing_status=Document.ProcessingStatus.REVIEW)
            .exclude(kind=Document.Kind.BANK_STATEMENT),
        "failed_documents": Document.objects.filter(
            processing_status=Document.ProcessingStatus.FAILED
        ),
        "mismatches": StatementImport.objects.filter(
            reconciliation_status=StatementImport.ReconciliationStatus.MISMATCH
        ).select_related("account", "document"),
        "uncategorized": Transaction.objects.filter(reviewed=True, category__isnull=True)
            .select_related("statement_import__account"),
    })


@login_required
def settings(request):
    email_config, _ = EmailImportConfig.objects.get_or_create(pk=1)
    return render(request, "ledger/settings.html", {
        "accounts": Account.objects.annotate(
            statement_count=Count("statementimport")
        ).order_by("name"),
        "account_form": AccountForm(),
        "email_form": EmailImportConfigForm(instance=email_config, prefix="email"),
        "email_config": email_config,
        "email_imports": EmailImportMessage.objects.filter(config=email_config)[:10],
    })


@login_required
def save_email_settings(request):
    if request.method != "POST":
        return redirect("settings")
    config, _ = EmailImportConfig.objects.get_or_create(pk=1)
    form = EmailImportConfigForm(request.POST, instance=config, prefix="email")
    if not form.is_valid():
        accounts = Account.objects.annotate(statement_count=Count("statementimport")).order_by("name")
        return render(request, "ledger/settings.html", {
            "accounts": accounts, "account_form": AccountForm(),
            "email_form": form, "email_config": config,
            "email_imports": EmailImportMessage.objects.filter(config=config)[:10],
        })
    config = form.save()
    action = request.POST.get("action")
    if action == "test":
        from .email_import import test_imap_connection
        try:
            test_imap_connection(config)
            messages.success(request, "IMAP-Verbindung und Ordner wurden erfolgreich geprüft.")
        except Exception as exc:
            messages.error(request, f"IMAP-Verbindung fehlgeschlagen: {exc}")
    elif action == "fetch":
        poll_email_import_task.delay(config.pk, force=True)
        messages.success(request, "Der manuelle E-Mail-Abruf wurde gestartet.")
    else:
        messages.success(request, "E-Mail-Importeinstellungen wurden gespeichert.")
    return redirect("settings")


@login_required
def add_account(request):
    if request.method != "POST":
        return redirect("settings")
    form = AccountForm(request.POST)
    if form.is_valid():
        account = form.save()
        messages.success(request, f"Konto „{account.name}“ wurde angelegt und kann ausgewählt werden.")
    else:
        details = " ".join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f"Das Konto konnte nicht angelegt werden: {details}")
    return redirect("settings")


@login_required
def edit_account(request, pk):
    account = get_object_or_404(Account, pk=pk)
    form = AccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        messages.success(request, f"Konto „{updated.name}“ wurde aktualisiert.")
        return redirect("settings")
    return render(request, "ledger/edit_account.html", {"form": form, "account": account})


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
                statement.document.processing_status = Document.ProcessingStatus.PROCESSED
                statement.document.save(update_fields=["processing_status", "updated_at"])
                rematched = refresh_unmatched_document_reviews()
                message = f"{queryset.count()} Buchungen wurden übernommen."
                if rematched:
                    message += f" Für {len(rematched)} Beleg(e) wurden mögliche Zuordnungen gefunden."
                messages.success(request, message)
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


@login_required
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
                Q(counterparty__icontains=values["q"])
                | Q(description__icontains=values["q"])
                | Q(comment__icontains=values["q"])
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
        form.instance.rule_suggestion = categorization_suggestion(form.instance)
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


@login_required
def manage_classification(request):
    forms = {
        "category": CategoryForm(prefix="category"),
        "tag": TagForm(prefix="tag"),
        "person": PersonForm(prefix="person"),
        "rule": CategorizationRuleForm(prefix="rule"),
    }
    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind == "apply_rules":
            scope = request.POST.get("scope", "uncategorized")
            try:
                rule_plan = build_rule_application_plan(scope)
            except ValueError:
                messages.error(request, "Der gewählte Anwendungsbereich ist ungültig.")
                return redirect("manage_classification")
            if request.POST.get("action") == "apply":
                applied_count = apply_rule_application_plan(rule_plan)
                messages.success(
                    request,
                    f"Regeln wurden auf {applied_count} Buchung(en) angewendet.",
                )
                return redirect("manage_classification")
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
        "rule_plan": locals().get("rule_plan"),
        "rule_scope": request.POST.get("scope", "uncategorized"),
    })


@login_required
def edit_rule(request, pk):
    rule = get_object_or_404(CategorizationRule, pk=pk)
    form = CategorizationRuleForm(request.POST or None, instance=rule)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Regel „{rule.name}“ wurde aktualisiert.")
        return redirect("manage_classification")
    return render(request, "ledger/edit_rule.html", {"form": form, "rule": rule})


@login_required
@require_POST
def delete_rule(request, pk):
    rule = get_object_or_404(CategorizationRule, pk=pk)
    rule_name = rule.name
    rule.delete()
    messages.success(request, f"Regel „{rule_name}“ wurde gelöscht.")
    return redirect("manage_classification")


@login_required
def edit_classification(request, kind, pk):
    editable = {
        "category": (Category, CategoryForm, "Kategorie"),
        "person": (Person, PersonForm, "Person"),
    }
    definition = editable.get(kind)
    if definition is None:
        return redirect("manage_classification")
    model, form_class, label = definition
    item = get_object_or_404(model, pk=pk)
    form = form_class(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        messages.success(request, f"{label} „{updated}“ wurde aktualisiert.")
        return redirect("manage_classification")
    return render(request, "ledger/edit_classification.html", {
        "form": form, "item": item, "label": label,
    })


@login_required
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


@login_required
def document_review(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("category").prefetch_related("tags", "people", "transactions"),
        pk=pk,
    )
    candidates = document_transaction_candidates(document)
    if request.method == "POST":
        if request.POST.get("action") == "retry":
            process_document_task.delay(document.pk)
            messages.success(request, "Erneute Analyse wurde vorgemerkt.")
            return redirect("document_review", pk=document.pk)
        review_form = DocumentReviewForm(request.POST, instance=document, prefix="document")
        link_form = DocumentTransactionLinkForm(
            request.POST, queryset=candidates, prefix="links"
        )
        if review_form.is_valid() and link_form.is_valid():
            document = review_form.save()
            document.transactions.set(link_form.cleaned_data["transactions"])
            document.processing_status = Document.ProcessingStatus.PROCESSED
            document.save(update_fields=["processing_status", "updated_at"])
            messages.success(request, "Dokument und Zuordnung wurden gespeichert.")
            return redirect("document_archive")
    else:
        review_form = DocumentReviewForm(instance=document, prefix="document")
        link_form = DocumentTransactionLinkForm(
            queryset=candidates,
            prefix="links",
            initial={"transactions": document.transactions.all()},
        )
    return render(request, "ledger/document_review.html", {
        "document": document,
        "review_form": review_form,
        "link_form": link_form,
    })


@login_required
def document_archive(request):
    queryset = Document.objects.select_related("category").prefetch_related(
        "tags", "people", "transactions"
    ).order_by("-document_date", "-created_at")
    filters = DocumentArchiveFilterForm(request.GET)
    if filters.is_valid():
        values = filters.cleaned_data
        if values.get("month"):
            try:
                year, month = map(int, values["month"].split("-"))
                queryset = queryset.filter(document_date__year=year, document_date__month=month)
            except (TypeError, ValueError):
                filters.add_error("month", "Bitte einen gültigen Monat auswählen.")
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
                | Q(original_filename__icontains=values["q"])
                | Q(extracted_text__icontains=values["q"])
            )
    documents = list(queryset.distinct())
    for document in documents:
        document.archive_month = (
            document.document_date.strftime("%m/%Y") if document.document_date else "Ohne Datum"
        )
    return render(request, "ledger/document_archive.html", {
        "documents": documents,
        "filters": filters,
    })


@login_required
def document_download(request, pk):
    document = get_object_or_404(Document, pk=pk)
    content_type = mimetypes.guess_type(document.original_filename)[0] or "application/octet-stream"
    return FileResponse(
        document.file.open("rb"),
        as_attachment=request.GET.get("download") == "1",
        filename=document.original_filename,
        content_type=content_type,
    )


def health(request):
    return HttpResponse("ok", content_type="text/plain")
