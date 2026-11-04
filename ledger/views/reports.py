import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import render

from ..forms import AnalyticsFilterForm, DocumentUploadForm
from ..models import Document, StatementImport, Transaction
from ..preferences import people_enabled
from ..presentation import base_context
from ..periods import (
    ZERO, booked_months, financial_transactions, income_expense_totals, latest_booking_date, month_end,
    month_choices, parse_month, previous_month,
)


@login_required
def dashboard(request):
    months = (
        Document.objects.exclude(document_date=None)
        .annotate(month=TruncMonth("document_date"))
        .values("month")
        .order_by("-month")
        .distinct()
    )
    month_start = parse_month(request.GET.get("month"), latest_booking_date())
    previous_start = previous_month(month_start)

    current = financial_transactions(month_start, month_end(month_start))
    previous = financial_transactions(previous_start, month_end(previous_start))
    totals = income_expense_totals(current)
    previous_expenses = abs(income_expense_totals(previous)["expenses"])
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
        "month_choices": month_choices(booked_months(month_start)),
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
        **base_context(totals["income"], totals["expenses"]),
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
    fallback = latest_booking_date()
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
    month_start = parse_month(selected_month, fallback)
    selected_month = month_start.strftime("%Y-%m")
    if period == "year":
        period_start = date(selected_year, 1, 1)
        period_end = date(selected_year, 12, 31)
        period_params = {"year": selected_year}
    else:
        period = "month"
        period_start = month_start
        period_end = month_end(month_start)
        period_params = {"month": selected_month}
    financial_queryset = financial_transactions(period_start, period_end)
    if account:
        financial_queryset = financial_queryset.filter(statement_import__account=account)
    totals = income_expense_totals(financial_queryset)
    queryset = financial_queryset.filter(amount__lt=0).select_related(
        "category", "statement_import__account"
    ).prefetch_related("people")
    transactions = list(queryset)
    total = sum((abs(item.amount) for item in transactions), Decimal("0"))

    show_people = people_enabled()
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
        if not show_people:
            continue
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
                income=Coalesce(Sum("amount", filter=Q(amount__gt=0)), ZERO),
                expenses=Coalesce(Sum("amount", filter=Q(amount__lt=0)), ZERO),
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
        **base_context(totals["income"], totals["expenses"]),
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
