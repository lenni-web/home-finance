from decimal import Decimal

from django.db.models import Sum

from .models import Budget, Category
from .periods import financial_transactions, month_end

WARNING_PERCENT = 80


def budget_overview(month_start):
    """Budgets gegen die Nettoausgaben des Monats (Erstattungen mindern die Ausgaben).

    Interne Umbuchungen und unbestätigte Buchungen zählen nicht mit.
    """
    spent_by_category = {
        row["category_id"]: -row["total"]
        for row in financial_transactions(month_start, month_end(month_start))
        .values("category_id").annotate(total=Sum("amount"))
    }
    rows = []
    for budget in Budget.objects.select_related("category"):
        spent = max(spent_by_category.pop(budget.category_id, Decimal("0")), Decimal("0"))
        percent = round(spent / budget.monthly_amount * 100)
        rows.append({
            "budget": budget,
            "category": budget.category,
            "limit": budget.monthly_amount,
            "spent": spent,
            "remaining": budget.monthly_amount - spent,
            "percent": percent,
            "bar_percent": min(percent, 100),
            "state": "over" if percent > 100 else "warn" if percent >= WARNING_PERCENT else "ok",
        })
    categories = Category.objects.in_bulk(key for key in spent_by_category if key is not None)
    unbudgeted = sorted(
        (
            {
                "category": categories.get(key),
                "name": categories[key].name if key in categories else "Ohne Kategorie",
                "spent": spent,
            }
            for key, spent in spent_by_category.items() if spent > 0
        ),
        key=lambda item: item["spent"], reverse=True,
    )
    total_limit = sum((row["limit"] for row in rows), Decimal("0"))
    total_spent = sum((row["spent"] for row in rows), Decimal("0"))
    return {
        "rows": rows,
        "unbudgeted": unbudgeted,
        "total_limit": total_limit,
        "total_spent": total_spent,
        "exceeded": sum(1 for row in rows if row["state"] == "over"),
    }
