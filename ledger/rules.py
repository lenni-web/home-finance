from django.db.models import F

from .models import CategorizationRule


def matching_rules(transaction, *, auto_only=False):
    rules = CategorizationRule.objects.filter(active=True).select_related("category").prefetch_related(
        "tags", "people"
    )
    if auto_only:
        rules = rules.filter(auto_apply=True)
    return [rule for rule in rules if rule.matches(transaction)]


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
