import re
from calendar import monthrange
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import render

from ..forms import AnalyticsFilterForm, DocumentUploadForm
from ..models import Document, StatementImport, Transaction


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
