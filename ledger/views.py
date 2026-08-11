from django.contrib import messages
from django.db.models.functions import TruncMonth
from django.shortcuts import redirect, render

from .forms import DocumentUploadForm
from .models import Document, Transaction


def dashboard(request):
    months = (
        Document.objects.exclude(document_date=None)
        .annotate(month=TruncMonth("document_date"))
        .values("month")
        .order_by("-month")
        .distinct()
    )
    return render(request, "ledger/dashboard.html", {
        "documents": Document.objects.select_related("category").prefetch_related("tags", "people")[:20],
        "transactions": Transaction.objects.select_related("category")[:10],
        "months": months,
        "upload_form": DocumentUploadForm(),
    })


def upload_document(request):
    if request.method != "POST":
        return redirect("dashboard")
    form = DocumentUploadForm(request.POST, request.FILES)
    if form.is_valid():
        form.save()
        messages.success(request, "Dokument wurde ins Archiv aufgenommen.")
    else:
        messages.error(request, "Das Dokument konnte nicht gespeichert werden.")
    return redirect("dashboard")

