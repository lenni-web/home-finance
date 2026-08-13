import hashlib

from django import forms
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.forms import modelformset_factory
from django.utils.formats import number_format

from .models import (
    Account, CategorizationRule, Category, Document, EmailImportConfig, Person, Tag, Transaction,
)


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


class EmailImportConfigForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        label="Passwort oder App-Passwort",
        widget=forms.PasswordInput(render_value=False),
        help_text="Leer lassen, um das bereits gespeicherte Passwort beizubehalten.",
    )

    class Meta:
        model = EmailImportConfig
        fields = [
            "enabled", "host", "port", "security", "username", "folder",
            "allowed_senders", "poll_interval_minutes", "mark_as_read",
        ]
        labels = {
            "enabled": "Automatischen Abruf aktivieren",
            "host": "IMAP-Server",
            "port": "Port",
            "security": "Verschlüsselung",
            "username": "Benutzername",
            "folder": "IMAP-Ordner",
            "allowed_senders": "Erlaubte Absender",
            "poll_interval_minutes": "Abrufintervall in Minuten",
            "mark_as_read": "Verarbeitete E-Mails als gelesen markieren",
        }
        widgets = {"allowed_senders": forms.Textarea(attrs={"rows": 4})}

    def clean_allowed_senders(self):
        value = self.cleaned_data["allowed_senders"]
        addresses = [
            item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()
        ]
        for address in addresses:
            try:
                validate_email(address)
            except ValidationError as exc:
                raise forms.ValidationError(f"Ungültige E-Mail-Adresse: {address}") from exc
        return "\n".join(addresses)

    def clean_port(self):
        port = self.cleaned_data["port"]
        if not 1 <= port <= 65535:
            raise forms.ValidationError("Bitte einen Port zwischen 1 und 65535 wählen.")
        return port

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("enabled"):
            for field in ("host", "username", "folder"):
                if not cleaned.get(field):
                    self.add_error(field, "Dieses Feld wird für den automatischen Abruf benötigt.")
            if not cleaned.get("password") and not self.instance.encrypted_password:
                self.add_error("password", "Bitte ein IMAP-Passwort eingeben.")
        interval = cleaned.get("poll_interval_minutes")
        if interval is not None and not 1 <= interval <= 1440:
            self.add_error("poll_interval_minutes", "Bitte 1 bis 1440 Minuten wählen.")
        return cleaned

    def save(self, commit=True):
        config = super().save(commit=False)
        if self.cleaned_data.get("password"):
            config.set_password(self.cleaned_data["password"])
        if commit:
            config.save()
        return config


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
        labels = {
            "file": "Datei",
            "kind": "Dokumenttyp",
            "title": "Titel (optional)",
            "document_date": "Dokumentdatum (optional)",
            "category": "Kategorie (optional)",
            "tags": "Tags (optional)",
        }
        widgets = {"document_date": forms.DateInput(attrs={"type": "date"})}

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 30 * 1024 * 1024:
            raise forms.ValidationError("Die Datei darf höchstens 30 MB groß sein.")
        if uploaded.content_type not in {"application/pdf", "image/jpeg", "image/png"}:
            raise forms.ValidationError("Erlaubt sind PDF-, JPEG- und PNG-Dateien.")
        header = uploaded.read(12)
        uploaded.seek(0)
        detected_type = None
        if header.startswith(b"%PDF-"):
            detected_type = "application/pdf"
        elif header.startswith(b"\xff\xd8\xff"):
            detected_type = "image/jpeg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            detected_type = "image/png"
        if detected_type != uploaded.content_type:
            raise forms.ValidationError(
                "Der tatsächliche Dateiinhalt stimmt nicht mit dem angegebenen Dateityp überein."
            )
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


class DocumentReviewForm(forms.ModelForm):
    people = forms.ModelMultipleChoiceField(
        queryset=Person.objects.filter(active=True), required=False,
        widget=forms.SelectMultiple(attrs={"size": 4}),
    )

    class Meta:
        model = Document
        fields = [
            "title", "document_date", "merchant", "invoice_number", "total_amount",
            "category", "tags",
        ]
        labels = {"invoice_number": "Rechnungsnummer"}
        widgets = {
            "document_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "tags": forms.SelectMultiple(attrs={"size": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["people"].initial = self.instance.people.all()

    def save(self, commit=True):
        document = super().save(commit=commit)
        if commit:
            document.people.set(self.cleaned_data["people"])
        return document


class DocumentTransactionLinkForm(forms.Form):
    transactions = forms.ModelMultipleChoiceField(
        queryset=Transaction.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Passende Kontobewegungen",
    )

    def __init__(self, *args, queryset=None, candidate_details=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidate_details = candidate_details or {}
        if queryset is not None:
            self.fields["transactions"].queryset = queryset
        self.fields["transactions"].label_from_instance = self.transaction_label

    def transaction_label(self, item):
        amount = number_format(item.amount, decimal_pos=2, use_l10n=True, force_grouping=True)
        details = self.candidate_details.get(item.pk, "")
        suffix = f" · {details}" if details else ""
        return f"{item.booking_date:%d.%m.%Y} · {item.counterparty} · {amount} €{suffix}"


class DocumentArchiveFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False, label="Von", widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        required=False, label="Bis", widget=forms.DateInput(attrs={"type": "date"})
    )
    month = forms.CharField(required=False, widget=forms.TextInput(attrs={"type": "month"}))
    kind = forms.ChoiceField(
        required=False, choices=[("", "Alle Dokumenttypen"), *Document.Kind.choices]
    )
    tag = forms.ModelChoiceField(queryset=Tag.objects.all(), required=False, empty_label="Alle Tags")
    person = forms.ModelChoiceField(
        queryset=Person.objects.all(), required=False, empty_label="Alle Personen"
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(), required=False, empty_label="Alle Kategorien"
    )
    link_status = forms.ChoiceField(
        required=False,
        label="Zuordnung",
        choices=[("", "Alle"), ("linked", "Zugeordnet"), ("unlinked", "Nicht zugeordnet")],
    )
    q = forms.CharField(
        required=False, label="Suche",
        widget=forms.TextInput(attrs={"placeholder": "Händler, Rechnungsnummer oder Datei"}),
    )


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


class TransactionFilterForm(forms.Form):
    month = forms.CharField(required=False, widget=forms.TextInput(attrs={"type": "month"}))
    year = forms.IntegerField(required=False, label="Jahr", min_value=2000, max_value=2100)
    account = forms.ModelChoiceField(
        queryset=Account.objects.all(), required=False, empty_label="Alle Konten"
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(), required=False, empty_label="Alle Kategorien"
    )
    tag = forms.ModelChoiceField(queryset=Tag.objects.all(), required=False, empty_label="Alle Tags")
    person = forms.ModelChoiceField(
        queryset=Person.objects.all(), required=False, empty_label="Alle Personen"
    )
    q = forms.CharField(required=False, label="Suche")
    uncategorized = forms.BooleanField(required=False, label="Nur ohne Kategorie")
    transfer_status = forms.ChoiceField(
        required=False,
        label="Umbuchungen",
        choices=[("", "Alle Buchungen"), ("internal", "Nur Umbuchungen"), ("open", "Ohne Gegenbuchung")],
    )


class AnalyticsFilterForm(forms.Form):
    period = forms.ChoiceField(
        required=False,
        label="Zeitraum",
        choices=[("month", "Monat"), ("year", "Jahr")],
        initial="month",
    )
    month = forms.CharField(
        required=False, label="Monat", widget=forms.TextInput(attrs={"type": "month"})
    )
    year = forms.IntegerField(
        required=False,
        label="Jahr",
        min_value=2000,
        max_value=2100,
        widget=forms.NumberInput(attrs={"step": 1}),
    )
    account = forms.ModelChoiceField(
        queryset=Account.objects.all(), required=False, label="Konto", empty_label="Alle Konten"
    )


class TransactionCategorizationForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ["category", "tags", "people", "comment", "is_internal_transfer"]
        labels = {"comment": "Kommentar", "is_internal_transfer": "Umbuchung"}
        widgets = {
            "tags": forms.SelectMultiple(attrs={"size": 3}),
            "people": forms.SelectMultiple(attrs={"size": 4}),
            "comment": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Ausgabe kurz erklären …",
            }),
        }


TransactionCategorizationFormSet = modelformset_factory(
    Transaction,
    form=TransactionCategorizationForm,
    extra=0,
)


class BulkCategorizationForm(forms.Form):
    category = forms.ModelChoiceField(
        queryset=Category.objects.filter(active=True), required=False, empty_label="Nicht ändern"
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.filter(active=True), required=False, widget=forms.SelectMultiple(attrs={"size": 3})
    )
    people = forms.ModelMultipleChoiceField(
        queryset=Person.objects.filter(active=True), required=False,
        widget=forms.SelectMultiple(attrs={"size": 4}),
    )
    transfer_action = forms.ChoiceField(
        required=False,
        label="Interne Umbuchung",
        choices=[("", "Nicht ändern"), ("mark", "Als Umbuchung markieren"), ("unmark", "Markierung aufheben")],
    )
    create_rules = forms.BooleanField(
        required=False, label="Für ausgewählte Zahlungspartner Regeln anlegen"
    )
    auto_apply = forms.BooleanField(
        required=False, initial=True, label="Neue Regeln künftig automatisch anwenden"
    )


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "color"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "color"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["name", "color"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class CategorizationRuleForm(forms.ModelForm):
    class Meta:
        model = CategorizationRule
        fields = [
            "name", "match_text", "priority", "category", "tags", "people",
            "marks_internal_transfer", "auto_apply",
        ]
        labels = {"priority": "Priorität (höher wird zuerst geprüft)"}


class CategorizationRuleFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label="Suche",
        widget=forms.TextInput(attrs={"placeholder": "Name, Suchtext oder Zuordnung"}),
    )
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[("", "Alle"), ("active", "Aktiv"), ("inactive", "Inaktiv")],
    )
    mode = forms.ChoiceField(
        required=False,
        label="Modus",
        choices=[("", "Alle"), ("automatic", "Automatisch"), ("suggestion", "Vorschlag")],
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=False,
        label="Kategorie",
        empty_label="Alle Kategorien",
    )
    sort = forms.ChoiceField(
        required=False,
        label="Sortierung",
        initial="priority_desc",
        choices=[
            ("priority_desc", "Priorität: hoch zuerst"),
            ("priority_asc", "Priorität: niedrig zuerst"),
            ("name", "Name: A–Z"),
            ("match_text", "Suchtext: A–Z"),
            ("applications", "Treffer: viele zuerst"),
            ("updated", "Zuletzt geändert"),
        ],
    )
