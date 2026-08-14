import mimetypes
import re
import shutil
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings as django_settings
from django.db import connection
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db.models.functions import TruncMonth
from django.http import FileResponse, HttpResponse
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.clickjacking import xframe_options_sameorigin

from .forms import (
    AccountForm, AnalyticsFilterForm, BulkCategorizationForm, CategorizationRuleFilterForm, CategorizationRuleForm, CategoryForm,
    DocumentArchiveFilterForm, DocumentReviewForm, DocumentTransactionLinkForm,
    DocumentUploadForm, EmailImportConfigForm, PersonForm, TagForm, TransactionCategorizationFormSet,
    TransactionFilterForm, TransactionReviewFormSet,
)
from .document_matching import (
    document_transaction_candidates, refresh_unmatched_document_reviews,
    scored_document_transaction_candidates,
)
from .importers import INGStatementParser
from .internal_transfers import best_transfer_candidate, link_transfer_pair, unlink_transfer
from .models import (
    Account, BackupRecord, CategorizationRule, Category, Document, EmailImportConfig,
    EmailImportMessage, Person, ServiceHeartbeat, StatementImport, Tag, Transaction,
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
    ).filter(is_internal_transfer=False)
    previous = Transaction.objects.filter(
        reviewed=True, booking_date__range=(previous_start, previous_end)
    ).filter(is_internal_transfer=False)
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
        "documents": Document.objects.select_related("category").prefetch_related("tags", "people")[:5],
        "transactions": Transaction.objects.select_related("category")[:10],
        "statement_imports": StatementImport.objects.select_related("document", "account")[:5],
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
        "uncategorized": Transaction.objects.filter(
            reviewed=True, category__isnull=True, is_internal_transfer=False
        ).count(),
        "mismatches": StatementImport.objects.filter(
            reconciliation_status=StatementImport.ReconciliationStatus.MISMATCH
        ).count(),
    }


def _chart_items(groups, total, period_params, account_id, filter_name):
    palette = ["#0f766e", "#2563eb", "#7c3aed", "#db2777", "#ea580c", "#65a30d"]
    items = []
    position = Decimal("0")
    gradient = []
    for index, group in enumerate(sorted(groups, key=lambda item: item["amount"], reverse=True)):
        amount = group["amount"]
        if not amount or not total:
            continue
        percent = amount / total * 100
        color = group.get("color") or palette[index % len(palette)]
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = palette[index % len(palette)]
        end = position + percent
        gradient.append(f"{color} {position:.2f}% {end:.2f}%")
        params = dict(period_params)
        if account_id:
            params["account"] = account_id
        if group.get("id") is None:
            if filter_name == "category":
                params["uncategorized"] = "on"
        else:
            params[filter_name] = group["id"]
        items.append({
            **group, "color": color, "percent": round(percent, 1),
            "drill_url": f"?{urlencode(params)}",
        })
        position = end
    return items, ", ".join(gradient) or "#e2e8f0 0% 100%"


@login_required
def analytics(request):
    latest_date = Transaction.objects.filter(reviewed=True).order_by("-booking_date").values_list(
        "booking_date", flat=True
    ).first()
    fallback = latest_date or date.today()
    filters = AnalyticsFilterForm(request.GET or {
        "period": "month", "month": fallback.strftime("%Y-%m"), "year": fallback.year,
    })
    selected_month = fallback.strftime("%Y-%m")
    selected_year = fallback.year
    period = "month"
    account = None
    if filters.is_valid():
        period = filters.cleaned_data.get("period") or "month"
        selected_month = filters.cleaned_data.get("month") or selected_month
        selected_year = filters.cleaned_data.get("year") or selected_year
        account = filters.cleaned_data.get("account")
    try:
        year, month = map(int, selected_month.split("-"))
        month_start = date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        month_start = date(fallback.year, fallback.month, 1)
        selected_month = month_start.strftime("%Y-%m")
    if period == "year":
        period_start = date(selected_year, 1, 1)
        period_end = date(selected_year, 12, 31)
        period_params = {"year": selected_year}
    else:
        period = "month"
        period_start = month_start
        period_end = date(
            month_start.year, month_start.month,
            monthrange(month_start.year, month_start.month)[1],
        )
        period_params = {"month": selected_month}
    financial_queryset = Transaction.objects.filter(
        reviewed=True, is_internal_transfer=False,
        booking_date__range=(period_start, period_end),
    )
    if account:
        financial_queryset = financial_queryset.filter(statement_import__account=account)
    zero = Value(Decimal("0.00"), output_field=DecimalField())
    totals = financial_queryset.aggregate(
        income=Coalesce(Sum("amount", filter=Q(amount__gt=0)), zero),
        expenses=Coalesce(Sum("amount", filter=Q(amount__lt=0)), zero),
        balance=Coalesce(Sum("amount"), zero),
    )
    queryset = financial_queryset.filter(amount__lt=0).select_related(
        "category", "statement_import__account"
    ).prefetch_related("people")
    transactions = list(queryset)
    total = sum((abs(item.amount) for item in transactions), Decimal("0"))

    category_groups = {}
    person_groups = {}
    for item in transactions:
        category_id = item.category_id
        category_groups.setdefault(category_id, {
            "id": category_id,
            "name": item.category.name if item.category else "Ohne Kategorie",
            "color": item.category.color if item.category else "#94a3b8",
            "amount": Decimal("0"),
        })["amount"] += abs(item.amount)
        people = list(item.people.all())
        if people:
            share = abs(item.amount) / len(people)
            for person in people:
                person_groups.setdefault(person.pk, {
                    "id": person.pk, "name": person.name, "color": person.color,
                    "amount": Decimal("0"),
                })["amount"] += share
        else:
            person_groups.setdefault(None, {
                "id": None, "name": "Ohne Person", "color": "#94a3b8",
                "amount": Decimal("0"),
            })["amount"] += abs(item.amount)

    account_id = account.pk if account else None
    category_items, category_gradient = _chart_items(
        category_groups.values(), total, period_params, account_id, "category"
    )
    person_items, person_gradient = _chart_items(
        person_groups.values(), total, period_params, account_id, "person"
    )
    monthly_rows = []
    if period == "year":
        monthly_totals = {
            item["month"].month: item
            for item in financial_queryset.annotate(month=TruncMonth("booking_date"))
            .values("month")
            .annotate(
                income=Coalesce(Sum("amount", filter=Q(amount__gt=0)), zero),
                expenses=Coalesce(Sum("amount", filter=Q(amount__lt=0)), zero),
            )
        }
        max_monthly_expenses = max(
            (abs(item["expenses"]) for item in monthly_totals.values()), default=Decimal("0")
        )
        for month_number in range(1, 13):
            values = monthly_totals.get(month_number, {})
            expenses = abs(values.get("expenses", Decimal("0")))
            monthly_rows.append({
                "date": date(selected_year, month_number, 1),
                "income": values.get("income", Decimal("0")),
                "expenses": expenses,
                "bar_percent": round(expenses / max_monthly_expenses * 100)
                if max_monthly_expenses else 0,
                "drill_url": f"?month={selected_year}-{month_number:02d}",
            })
    return render(request, "ledger/analytics.html", {
        "filters": filters, "selected_month": selected_month, "month_date": month_start,
        "selected_year": selected_year, "period": period,
        "period_label": selected_year if period == "year" else month_start,
        "total": total, "totals": totals, "expenses_absolute": abs(totals["expenses"]),
        "transaction_count": len(transactions), "monthly_rows": monthly_rows,
        "category_items": category_items, "category_gradient": category_gradient,
        "person_items": person_items, "person_gradient": person_gradient,
    })


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
        .select_related(
            "category", "statement_import__account",
            "transfer_counterpart__statement_import__account",
        )
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
        elif values.get("year"):
            queryset = queryset.filter(booking_date__year=values["year"])
        if values.get("account"):
            queryset = queryset.filter(statement_import__account=values["account"])
        if values.get("category"):
            queryset = queryset.filter(category=values["category"])
        if values.get("tag"):
            queryset = queryset.filter(tags=values["tag"])
        if values.get("person"):
            queryset = queryset.filter(people=values["person"])
        if values.get("uncategorized"):
            queryset = queryset.filter(category__isnull=True, is_internal_transfer=False)
        if values.get("transfer_status") == "internal":
            queryset = queryset.filter(is_internal_transfer=True)
        elif values.get("transfer_status") == "open":
            queryset = queryset.filter(is_internal_transfer=True, transfer_counterpart__isnull=True)
        if values.get("q"):
            queryset = queryset.filter(
                Q(counterparty__icontains=values["q"])
                | Q(description__icontains=values["q"])
                | Q(comment__icontains=values["q"])
            )
    queryset = queryset.distinct()
    transaction_count = queryset.count()
    per_page = request.GET.get("per_page", "30")
    if per_page not in {"30", "50", "100", "all"}:
        per_page = "30"
    page_obj = None
    pagination_links = []
    if per_page == "all":
        display_queryset = queryset
        displayed_from = 1 if transaction_count else 0
        displayed_to = transaction_count
    else:
        paginator = Paginator(queryset, int(per_page))
        page_obj = paginator.get_page(request.GET.get("page", 1))
        display_queryset = page_obj.object_list
        displayed_from = page_obj.start_index()
        displayed_to = page_obj.end_index()
        for page_number in paginator.get_elided_page_range(
            page_obj.number, on_each_side=2, on_ends=1
        ):
            if page_number == paginator.ELLIPSIS:
                pagination_links.append({"ellipsis": True})
                continue
            params = request.GET.copy()
            params["page"] = page_number
            pagination_links.append({
                "number": page_number,
                "current": page_number == page_obj.number,
                "url": f"?{params.urlencode()}",
            })
    per_page_options = []
    for value, label in [("30", "30"), ("50", "50"), ("100", "100"), ("all", "Alle")]:
        params = request.GET.copy()
        params["per_page"] = value
        params.pop("page", None)
        per_page_options.append({
            "value": value, "label": label, "url": f"?{params.urlencode()}",
        })

    if request.method == "POST":
        formset = TransactionCategorizationFormSet(
            request.POST, queryset=display_queryset, prefix="transactions"
        )
        bulk_form = BulkCategorizationForm(request.POST, prefix="bulk")
        action = request.POST.get("action")
        if action and action.startswith("confirm_transfer:"):
            try:
                first_id, second_id = map(int, action.split(":")[1:])
                first = queryset.get(pk=first_id)
                second = Transaction.objects.select_related("statement_import__account").get(
                    pk=second_id, reviewed=True
                )
                link_transfer_pair(first, second)
                messages.success(request, "Die beiden Buchungen wurden als Umbuchung verknüpft.")
            except (ValueError, Transaction.DoesNotExist):
                messages.error(request, "Dieses Umbuchungspaar ist nicht mehr gültig.")
            return redirect(request.get_full_path())
        if formset.is_valid() and bulk_form.is_valid():
            formset.save()
            for form in formset.forms:
                if "is_internal_transfer" in form.changed_data and not form.instance.is_internal_transfer:
                    unlink_transfer(form.instance)
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
                        if bulk.get("transfer_action") == "mark":
                            item.is_internal_transfer = True
                            item.save(update_fields=["is_internal_transfer", "updated_at"])
                        elif bulk.get("transfer_action") == "unmark":
                            unlink_transfer(item)
                        item.tags.add(*bulk["tags"])
                        item.tags.remove(*bulk["remove_tags"])
                        item.people.add(*bulk["people"])
                    if bulk.get("create_rules"):
                        _create_rules_from_transactions(selected, bulk)
                    messages.success(request, f"{selected.count()} Buchungen wurden bearbeitet.")
                    return redirect(request.get_full_path())
            else:
                messages.success(request, "Zuordnungen wurden gespeichert.")
                return redirect(request.get_full_path())
    else:
        formset = TransactionCategorizationFormSet(
            queryset=display_queryset, prefix="transactions"
        )
        bulk_form = BulkCategorizationForm(prefix="bulk")

    report_queryset = queryset.filter(is_internal_transfer=False)
    totals = report_queryset.aggregate(
        income=Sum("amount", filter=Q(amount__gt=0), default=0),
        expenses=Sum("amount", filter=Q(amount__lt=0), default=0),
        balance=Sum("amount", default=0),
    )
    for form in formset.forms:
        form.instance.rule_suggestion = categorization_suggestion(form.instance)
        form.instance.transfer_suggestion = (
            None if form.instance.transfer_counterpart_id
            else best_transfer_candidate(form.instance)
        )
    return render(request, "ledger/transaction_overview.html", {
        "filters": filters,
        "formset": formset,
        "bulk_form": bulk_form,
        "totals": totals,
        "transaction_count": transaction_count,
        "displayed_from": displayed_from,
        "displayed_to": displayed_to,
        "per_page": per_page,
        "per_page_options": per_page_options,
        "page_obj": page_obj,
        "pagination_links": pagination_links,
        "uncategorized_count": queryset.filter(
            category__isnull=True, is_internal_transfer=False
        ).count(),
        "transfer_count": queryset.filter(is_internal_transfer=True).count(),
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
                marks_internal_transfer=bulk.get("transfer_action") == "mark",
                auto_apply=bulk.get("auto_apply", False),
            )
        elif bulk.get("category") or bulk.get("transfer_action") == "mark":
            if bulk.get("category"):
                rule.category = bulk["category"]
            if bulk.get("transfer_action") == "mark":
                rule.marks_internal_transfer = True
            rule.auto_apply = bulk.get("auto_apply", False)
            rule.save(update_fields=[
                "category", "marks_internal_transfer", "auto_apply", "updated_at"
            ])
        rule.tags.add(*bulk["tags"])
        rule.tags.remove(*bulk["remove_tags"])
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
    rules = CategorizationRule.objects.select_related("category").prefetch_related(
        "tags", "people"
    )
    rule_count = rules.count()
    rule_filters = CategorizationRuleFilterForm(request.GET)
    if rule_filters.is_valid():
        values = rule_filters.cleaned_data
        if values.get("q"):
            query = values["q"]
            rules = rules.filter(
                Q(name__icontains=query)
                | Q(match_text__icontains=query)
                | Q(category__name__icontains=query)
                | Q(tags__name__icontains=query)
                | Q(people__name__icontains=query)
            )
        if values.get("status") == "active":
            rules = rules.filter(active=True)
        elif values.get("status") == "inactive":
            rules = rules.filter(active=False)
        if values.get("mode") == "automatic":
            rules = rules.filter(auto_apply=True)
        elif values.get("mode") == "suggestion":
            rules = rules.filter(auto_apply=False)
        if values.get("category"):
            rules = rules.filter(category=values["category"])
        ordering = {
            "priority_desc": ("-priority", "name"),
            "priority_asc": ("priority", "name"),
            "name": ("name",),
            "match_text": ("match_text",),
            "applications": ("-times_applied", "name"),
            "updated": ("-updated_at",),
        }.get(values.get("sort"), ("-priority", "name"))
        rules = rules.order_by(*ordering)
    rules = rules.distinct()
    return render(request, "ledger/manage_classification.html", {
        "forms": forms,
        "categories": Category.objects.order_by("name"),
        "tags": Tag.objects.order_by("name"),
        "people": Person.objects.order_by("name"),
        "rules": rules,
        "rule_count": rule_count,
        "filtered_rule_count": rules.count(),
        "rule_filters": rule_filters,
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
    grouped_documents = {}
    for document in queryset.distinct():
        month_label = (
            document.document_date.strftime("%m/%Y") if document.document_date else "Ohne Datum"
        )
        month_key = (
            document.document_date.strftime("%Y_%m") if document.document_date else "undated"
        )
        group = grouped_documents.setdefault(month_key, {
            "key": month_key, "label": month_label, "documents": [],
        })
        group["documents"].append(document)
    archive_groups = []
    for group in grouped_documents.values():
        paginator = Paginator(group["documents"], 10)
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


@login_required
def operational_status(request):
    now = timezone.now()
    worker = ServiceHeartbeat.objects.filter(name="worker").first()
    email_heartbeat = ServiceHeartbeat.objects.filter(name="email_import").first()
    latest_backup = BackupRecord.objects.first()
    email_config = EmailImportConfig.objects.first()
    failed_documents = Document.objects.filter(
        processing_status=Document.ProcessingStatus.FAILED
    ).count()
    disk = shutil.disk_usage(django_settings.MEDIA_ROOT)
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database_ok = False

    worker_ok = bool(worker and worker.last_seen_at >= now - timedelta(minutes=3))
    backup_ok = bool(latest_backup and latest_backup.created_at >= now - timedelta(hours=26))
    email_ok = not email_config or not email_config.enabled or (
        not email_config.last_error
        and email_config.last_success_at
        and email_config.last_success_at >= now - timedelta(
            minutes=max(email_config.poll_interval_minutes * 3, 15)
        )
    )
    checks = [
        ("Datenbank", database_ok, "Verbindung erfolgreich" if database_ok else "Nicht erreichbar"),
        (
            "Hintergrund-Worker", worker_ok,
            "Letztes Signal "
            f"{timezone.localtime(worker.last_seen_at):%d.%m.%Y %H:%M}"
            if worker else "Noch kein Signal",
        ),
        (
            "Automatisches Backup", backup_ok,
            f"{latest_backup.filename} · "
            f"{timezone.localtime(latest_backup.created_at):%d.%m.%Y %H:%M}"
            if latest_backup else "Noch kein protokolliertes Backup",
        ),
        (
            "E-Mail-Import", email_ok,
            "Nicht aktiviert" if not email_config or not email_config.enabled
            else (email_config.last_error or "Letzter Abruf erfolgreich"),
        ),
    ]
    return render(request, "ledger/operational_status.html", {
        "checks": checks,
        "all_ok": all(item[1] for item in checks),
        "failed_documents": failed_documents,
        "disk_free_gb": disk.free / (1024 ** 3),
        "disk_percent_free": disk.free / disk.total * 100,
        "revision": django_settings.DEPLOY_REVISION,
        "version": django_settings.DEPLOY_TAG,
        "worker": worker,
        "email_heartbeat": email_heartbeat,
        "latest_backup": latest_backup,
    })


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
