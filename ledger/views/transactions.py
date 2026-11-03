from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import redirect, render

from ..forms import BulkCategorizationForm, TransactionCategorizationFormSet, TransactionFilterForm
from ..internal_transfers import best_transfer_candidates, link_transfer_pair, unlink_transfer
from ..models import CategorizationRule, Transaction
from ..presentation import base_context
from ..rules import categorization_suggestions


def _filtered_transactions(request):
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
    return queryset.distinct(), filters


def _paginate_transactions(request, queryset, transaction_count):
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
    return {
        "display_queryset": display_queryset,
        "page_obj": page_obj,
        "pagination_links": pagination_links,
        "displayed_from": displayed_from,
        "displayed_to": displayed_to,
        "per_page": per_page,
        "per_page_options": per_page_options,
    }


def _apply_bulk_action(request, queryset, bulk_form):
    """Wendet die Sammelzuordnung an; gibt eine Weiterleitung oder ``None`` zurück."""
    selected_ids = request.POST.getlist("selected")
    selected = queryset.filter(pk__in=selected_ids)
    bulk = bulk_form.cleaned_data
    if not selected_ids:
        messages.error(request, "Bitte mindestens eine Buchung auswählen.")
        return None
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


def _handle_post(request, queryset, formset, bulk_form):
    """Verarbeitet das Formular; gibt eine Weiterleitung oder ``None`` zurück."""
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
    if not (formset.is_valid() and bulk_form.is_valid()):
        return None
    formset.save()
    for form in formset.forms:
        if "is_internal_transfer" in form.changed_data and not form.instance.is_internal_transfer:
            unlink_transfer(form.instance)
    if action == "bulk":
        return _apply_bulk_action(request, queryset, bulk_form)
    messages.success(request, "Zuordnungen wurden gespeichert.")
    return redirect(request.get_full_path())


@login_required
def transaction_overview(request):
    queryset, filters = _filtered_transactions(request)
    transaction_count = queryset.count()
    paging = _paginate_transactions(request, queryset, transaction_count)
    display_queryset = paging.pop("display_queryset")

    if request.method == "POST":
        formset = TransactionCategorizationFormSet(
            request.POST, queryset=display_queryset, prefix="transactions"
        )
        bulk_form = BulkCategorizationForm(request.POST, prefix="bulk")
        response = _handle_post(request, queryset, formset, bulk_form)
        if response:
            return response
    else:
        formset = TransactionCategorizationFormSet(
            queryset=display_queryset, prefix="transactions"
        )
        bulk_form = BulkCategorizationForm(prefix="bulk")

    totals = queryset.filter(is_internal_transfer=False).aggregate(
        income=Sum("amount", filter=Q(amount__gt=0), default=0),
        expenses=Sum("amount", filter=Q(amount__lt=0), default=0),
        balance=Sum("amount", default=0),
    )
    instances = [form.instance for form in formset.forms]
    rule_suggestions = categorization_suggestions(instances)
    transfer_suggestions = best_transfer_candidates(
        [item for item in instances if not item.transfer_counterpart_id]
    )
    for item in instances:
        item.rule_suggestion = rule_suggestions.get(item.pk)
        item.transfer_suggestion = transfer_suggestions.get(item.pk)
    return render(request, "ledger/transaction_overview.html", {
        "filters": filters,
        "formset": formset,
        "bulk_form": bulk_form,
        "totals": totals,
        "transaction_count": transaction_count,
        **paging,
        "uncategorized_count": queryset.filter(
            category__isnull=True, is_internal_transfer=False
        ).count(),
        "transfer_count": queryset.filter(is_internal_transfer=True).count(),
        **base_context(totals["income"], totals["expenses"]),
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
