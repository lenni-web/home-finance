from dataclasses import dataclass

from django.db import transaction as db_transaction
from django.db.models import F

from .models import CategorizationRule, Transaction
from .text_normalization import normalize_comparison_text


PAYMENT_NOISE = {
    "gmbh", "ag", "kg", "se", "ug", "mbh", "de", "deutschland",
    "zahlung", "kartenzahlung", "lastschrift", "visa", "mastercard",
}
MIN_LEARNING_SAMPLES = 3
PAYMENT_INTERMEDIARIES = {
    "paypal", "klarna", "stripe", "sumup", "mollie", "adyen",
}


def normalize_merchant(value):
    """Create a stable, conservative key without altering the stored bank text."""
    words = normalize_comparison_text(value).split()
    relevant = [word for word in words if word not in PAYMENT_NOISE and not word.isdigit()]
    return " ".join(relevant[:8])


def is_payment_intermediary(value):
    words = normalize_comparison_text(value).split()
    return bool(words and words[0] in PAYMENT_INTERMEDIARIES)


@dataclass(frozen=True)
class RuleSuggestion:
    category: object
    confidence: int
    source: str
    sample_count: int
    rule: object = None

    @property
    def name(self):
        return self.rule.name if self.rule else f"Erlernt aus {self.sample_count} Buchungen"


@dataclass
class RuleApplication:
    transaction: object
    rules: list
    category: object
    tag_ids: set
    person_ids: set
    marks_internal_transfer: bool = False

    @property
    def rule_names(self):
        return ", ".join(rule.name for rule in self.rules)


def matching_rules(transaction, *, auto_only=False):
    rules = CategorizationRule.objects.filter(active=True).select_related("category").prefetch_related(
        "tags", "people"
    )
    if auto_only:
        rules = rules.filter(auto_apply=True)
    return [rule for rule in rules.order_by("-priority", "name") if rule.matches(transaction)]


def categorization_suggestion(transaction):
    rules = matching_rules(transaction)
    if rules:
        rule = rules[0]
        specificity = min(len(normalize_merchant(rule.match_text)) * 2, 25)
        return RuleSuggestion(
            category=rule.category,
            confidence=min(99, 70 + specificity),
            source="Regel",
            sample_count=rule.times_applied,
            rule=rule,
        )

    merchant_key = normalize_merchant(transaction.counterparty)
    if not merchant_key or is_payment_intermediary(transaction.counterparty):
        return None
    candidates = Transaction.objects.filter(
        reviewed=True, category__isnull=False
    ).exclude(pk=transaction.pk).select_related("category")
    same_merchant = [item for item in candidates if normalize_merchant(item.counterparty) == merchant_key]
    if len(same_merchant) < MIN_LEARNING_SAMPLES:
        return None
    category_counts = {}
    categories = {}
    for item in same_merchant:
        category_counts[item.category_id] = category_counts.get(item.category_id, 0) + 1
        categories[item.category_id] = item.category
    category_id, count = max(category_counts.items(), key=lambda pair: pair[1])
    confidence = round(count / len(same_merchant) * 100)
    if confidence < 60:
        return None
    return RuleSuggestion(
        category=categories[category_id], confidence=confidence,
        source="Bestätigte Buchungen", sample_count=len(same_merchant),
    )


def apply_categorization_rules(transaction):
    applied = []
    for rule in matching_rules(transaction, auto_only=True):
        changed_fields = []
        if rule.category_id and not transaction.category_id:
            transaction.category = rule.category
            changed_fields.append("category")
        if rule.marks_internal_transfer and not transaction.is_internal_transfer:
            transaction.is_internal_transfer = True
            changed_fields.append("is_internal_transfer")
        if changed_fields:
            transaction.save(update_fields=changed_fields + ["updated_at"])
        transaction.tags.add(*rule.tags.all())
        transaction.people.add(*rule.people.all())
        CategorizationRule.objects.filter(pk=rule.pk).update(times_applied=F("times_applied") + 1)
        applied.append(rule)
    return applied


def build_rule_application_plan(scope="uncategorized"):
    queryset = Transaction.objects.filter(reviewed=True).select_related(
        "category", "statement_import__account"
    ).prefetch_related("tags", "people").order_by("-booking_date", "-id")
    if scope == "uncategorized":
        queryset = queryset.filter(category__isnull=True)
    elif scope != "all":
        raise ValueError("Ungültiger Anwendungsbereich für Regeln.")

    rules = list(
        CategorizationRule.objects.filter(active=True, auto_apply=True)
        .select_related("category").prefetch_related("tags", "people")
        .order_by("-priority", "name")
    )
    plan = []
    for item in queryset:
        matches = [rule for rule in rules if rule.matches(item)]
        if not matches:
            continue
        category = next((rule.category for rule in matches if rule.category_id), None)
        tag_ids = {tag.pk for rule in matches for tag in rule.tags.all()}
        person_ids = {person.pk for rule in matches for person in rule.people.all()}
        marks_internal_transfer = any(rule.marks_internal_transfer for rule in matches)
        existing_tag_ids = {tag.pk for tag in item.tags.all()}
        existing_person_ids = {person.pk for person in item.people.all()}
        if (
            (category and category.pk != item.category_id)
            or not tag_ids.issubset(existing_tag_ids)
            or not person_ids.issubset(existing_person_ids)
            or (marks_internal_transfer and not item.is_internal_transfer)
        ):
            plan.append(RuleApplication(
                item, matches, category, tag_ids, person_ids, marks_internal_transfer
            ))
    return plan


def apply_rule_application_plan(plan):
    with db_transaction.atomic():
        for application in plan:
            item = application.transaction
            if application.category and item.category_id != application.category.pk:
                item.category = application.category
                item.save(update_fields=["category", "updated_at"])
            if application.marks_internal_transfer and not item.is_internal_transfer:
                item.is_internal_transfer = True
                item.save(update_fields=["is_internal_transfer", "updated_at"])
            if application.tag_ids:
                item.tags.add(*application.tag_ids)
            if application.person_ids:
                item.people.add(*application.person_ids)
            for rule in application.rules:
                CategorizationRule.objects.filter(pk=rule.pk).update(
                    times_applied=F("times_applied") + 1
                )
    return len(plan)
