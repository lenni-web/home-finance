from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from ..document_matching import refresh_unmatched_document_reviews
from ..forms import TransactionReviewFormSet
from ..models import Document, StatementImport


@login_required
def statement_review(request, pk):
    statement = get_object_or_404(
        StatementImport.objects.select_related("document", "account"), pk=pk
    )
    queryset = statement.transactions.select_related("category").order_by("source_page", "id")
    if request.method == "POST" and statement.status == StatementImport.Status.REVIEW:
        formset = TransactionReviewFormSet(request.POST, queryset=queryset, prefix="transactions")
        if formset.is_valid():
            transactions = formset.save(commit=False)
            confirm = request.POST.get("action") == "confirm"
            for item in transactions:
                item.save()
            formset.save_m2m()
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
