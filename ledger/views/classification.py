from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import (
    CategorizationRuleFilterForm, CategorizationRuleForm, CategoryForm, PersonForm, TagForm,
)
from ..models import CategorizationRule, Category, Person, Tag
from ..rules import apply_rule_application_plan, build_rule_application_plan


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
