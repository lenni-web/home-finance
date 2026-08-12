import re
from dataclasses import dataclass

from django.db.models import F

from .models import CategorizationRule, Transaction


PAYMENT_NOISE = {
    "gmbh", "ag", "kg", "se", "ug", "mbh", "de", "deutschland",
    "zahlung", "kartenzahlung", "lastschrift", "visa", "mastercard",
}


def normalize_merchant(value):
    """Create a stable, conservative key without altering the stored bank text."""
    words = re.findall(r"[a-z0-9äöüß]+", (value or "").casefold())
    relevant = [word for word in words if word not in PAYMENT_NOISE and not word.isdigit()]
    return " ".join(relevant[:8])


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
    if not merchant_key:
        return None
    candidates = Transaction.objects.filter(
        reviewed=True, category__isnull=False
    ).exclude(pk=transaction.pk).select_related("category")
    same_merchant = [item for item in candidates if normalize_merchant(item.counterparty) == merchant_key]
    if not same_merchant:
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
        if changed_fields:
            transaction.save(update_fields=changed_fields + ["updated_at"])
        transaction.tags.add(*rule.tags.all())
        transaction.people.add(*rule.people.all())
        CategorizationRule.objects.filter(pk=rule.pk).update(times_applied=F("times_applied") + 1)
        applied.append(rule)
    return applied
