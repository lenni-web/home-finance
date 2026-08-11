import hashlib

from django import forms
from django.forms import modelformset_factory

from .models import Account, Document, Transaction


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["name", "iban_last_four"]
        labels = {
            "name": "Kontoname",
            "iban_last_four": "Letzte vier Stellen der IBAN (optional)",
        }

    def clean_iban_last_four(self):
        value = self.cleaned_data["iban_last_four"].strip()
        if value and (len(value) != 4 or not value.isdigit()):
            raise forms.ValidationError("Bitte genau vier Ziffern eingeben.")
        return value


class DocumentUploadForm(forms.ModelForm):
    account = forms.ModelChoiceField(
        queryset=Account.objects.all(),
        required=False,
        label="Konto (nur bei Kontoauszügen)",
        empty_label="Konto auswählen",
    )

    class Meta:
        model = Document
        fields = ["file", "kind", "title", "document_date", "category", "tags"]
        widgets = {"document_date": forms.DateInput(attrs={"type": "date"})}

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 30 * 1024 * 1024:
            raise forms.ValidationError("Die Datei darf höchstens 30 MB groß sein.")
        if uploaded.content_type not in {"application/pdf", "image/jpeg", "image/png"}:
            raise forms.ValidationError("Erlaubt sind PDF-, JPEG- und PNG-Dateien.")
        digest = hashlib.sha256()
        for chunk in uploaded.chunks():
            digest.update(chunk)
        uploaded.seek(0)
        if Document.objects.filter(sha256=digest.hexdigest()).exists():
            raise forms.ValidationError("Diese Datei wurde bereits hochgeladen.")
        return uploaded

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == Document.Kind.BANK_STATEMENT:
            if not cleaned.get("account"):
                self.add_error("account", "Für einen Kontoauszug muss ein Konto gewählt werden.")
            uploaded = cleaned.get("file")
            if uploaded and uploaded.content_type != "application/pdf":
                self.add_error("file", "Kontoauszüge müssen als PDF hochgeladen werden.")
        return cleaned


class TransactionReviewForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "booking_date", "value_date", "booking_type", "counterparty",
            "description", "amount", "category",
        ]
        widgets = {
            "booking_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "value_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }


TransactionReviewFormSet = modelformset_factory(
    Transaction,
    form=TransactionReviewForm,
    extra=0,
)
