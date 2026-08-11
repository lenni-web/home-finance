from django import forms

from .models import Document


class DocumentUploadForm(forms.ModelForm):
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
        return uploaded

