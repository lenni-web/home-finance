from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from ..document_matching import refresh_unmatched_document_reviews
from ..forms import TransactionReviewFormSet
from ..importers import get_parser
from ..models import Document, StatementImport
from ..payees import learn_payee, resplit_pending
from ..presentation import hidden_in_presentation


def _learn_payees(statement, formset):
    """Merkt gekürzte Zahlungspartner-Namen und verschiebt den Rest in den Betreff."""
    if not getattr(get_parser(statement.parser_name), "splits_payee", False):
        return []
    learned = []
    for form in formset.forms:
        if "counterparty" not in form.changed_data:
            continue
        result = learn_payee(
            form.initial.get("counterparty", ""), form.initial.get("description", ""),
            form.cleaned_data["counterparty"],
        )
        if result is None:
            continue
        remainder, name = result
        learned.append(name)
        if "description" not in form.changed_data:
            form.instance.description = remainder
    return learned


@login_required
@hidden_in_presentation()
def statement_review(request, pk):
    statement = get_object_or_404(
        StatementImport.objects.select_related("document", "account"), pk=pk
    )
    queryset = statement.transactions.select_related("category").order_by("source_page", "id")
    if request.method == "POST" and statement.status == StatementImport.Status.REVIEW:
        formset = TransactionReviewFormSet(request.POST, queryset=queryset, prefix="transactions")
        if formset.is_valid():
            learned = _learn_payees(statement, formset)
            transactions = formset.save(commit=False)
            confirm = request.POST.get("action") == "confirm"
            for item in transactions:
                item.save()
            formset.save_m2m()
            if learned:
                edited = {form.instance.pk for form in formset.forms if form.has_changed()}
                others = resplit_pending(statement, exclude_ids=edited)
                names = ", ".join(f"„{name}“" for name in dict.fromkeys(learned))
                messages.success(
                    request,
                    f"Zahlungspartner gemerkt: {names}. Bei {others} weiteren Buchung(en) wurde der "
                    "Betreff abgetrennt. Künftige Importe trennen diesen Namen automatisch.",
                )
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
