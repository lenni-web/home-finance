from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..budgets import budget_overview
from ..forms import BudgetForm
from ..models import Budget
from ..periods import booked_months, latest_booking_date, month_choices, parse_month


def _month_url(month_start):
    return f"{reverse('budgets')}?{urlencode({'month': month_start.strftime('%Y-%m')})}"


@login_required
def budgets(request):
    month_start = parse_month(request.GET.get("month"), latest_booking_date())
    if request.method == "POST":
        form = BudgetForm(request.POST)
        if form.is_valid():
            budget = form.save()
            messages.success(request, f"Budget für „{budget.category}“ wurde gespeichert.")
            return redirect(_month_url(month_start))
    else:
        form = BudgetForm()
    return render(request, "ledger/budgets.html", {
        "form": form,
        "selected_month": month_start.strftime("%Y-%m"),
        "month_date": month_start,
        "month_choices": month_choices(booked_months(month_start)),
        "overview": budget_overview(month_start),
    })


@login_required
@require_POST
def delete_budget(request, pk):
    budget = Budget.objects.filter(pk=pk).select_related("category").first()
    month_start = parse_month(request.POST.get("month"), latest_booking_date())
    if budget:
        budget.delete()
        messages.success(request, f"Budget für „{budget.category}“ wurde entfernt.")
    return redirect(_month_url(month_start))
