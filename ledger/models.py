import hashlib
from pathlib import Path

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Account(TimestampedModel):
    name = models.CharField(max_length=120)
    iban_last_four = models.CharField(max_length=4, blank=True)

    def __str__(self):
        return self.name


class Category(TimestampedModel):
    name = models.CharField(max_length=100, unique=True)
    color = models.CharField(max_length=7, default="#64748b")

    def __str__(self):
        return self.name


class Tag(TimestampedModel):
    name = models.CharField(max_length=80, unique=True)
    color = models.CharField(max_length=7, default="#0f766e")

    def __str__(self):
        return self.name


class Person(TimestampedModel):
    name = models.CharField(max_length=120, unique=True)
    color = models.CharField(max_length=7, default="#7c3aed")
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


def document_path(instance, filename):
    date = instance.document_date or timezone.localdate()
    return f"documents/{date:%Y/%m}/{filename}"


class Document(TimestampedModel):
    class Kind(models.TextChoices):
        BANK_STATEMENT = "bank_statement", "Kontoauszug"
        INVOICE = "invoice", "Rechnung"
        RECEIPT = "receipt", "Kassenbeleg"
        OTHER = "other", "Sonstiges"

    title = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    file = models.FileField(upload_to=document_path)
    original_filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True, editable=False)
    document_date = models.DateField(null=True, blank=True, db_index=True)
    merchant = models.CharField(max_length=255, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    extracted_text = models.TextField(blank=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
    tags = models.ManyToManyField(Tag, blank=True, related_name="documents")
    people = models.ManyToManyField(Person, through="DocumentPerson", related_name="documents")

    class Meta:
        ordering = ["-document_date", "-created_at"]

    def calculate_sha256(self):
        digest = hashlib.sha256()
        for chunk in self.file.chunks():
            digest.update(chunk)
        self.file.seek(0)
        return digest.hexdigest()

    def save(self, *args, **kwargs):
        if self.file and not self.sha256:
            self.sha256 = self.calculate_sha256()
        if not self.original_filename and self.file:
            self.original_filename = Path(self.file.name).name
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title or self.original_filename


class DocumentPerson(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Zugeordnet"
        PAYER = "payer", "Zahler/in"
        BENEFICIARY = "beneficiary", "Begünstigte Person"

    document = models.ForeignKey(Document, on_delete=models.CASCADE)
    person = models.ForeignKey(Person, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OWNER)
    share_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["document", "person", "role"], name="unique_document_person_role"
        )]


class StatementImport(TimestampedModel):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Hochgeladen"
        PROCESSING = "processing", "Wird verarbeitet"
        REVIEW = "review", "Prüfung erforderlich"
        IMPORTED = "imported", "Importiert"
        FAILED = "failed", "Fehlgeschlagen"

    document = models.OneToOneField(Document, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    parser_name = models.CharField(max_length=100, blank=True)
    parser_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED)
    error_message = models.TextField(blank=True)


class Transaction(TimestampedModel):
    statement_import = models.ForeignKey(
        StatementImport, on_delete=models.PROTECT, related_name="transactions"
    )
    booking_date = models.DateField(db_index=True)
    value_date = models.DateField(null=True, blank=True)
    description = models.TextField()
    counterparty = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    source_page = models.PositiveIntegerField(null=True, blank=True)
    source_fingerprint = models.CharField(max_length=64, unique=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
    tags = models.ManyToManyField(Tag, blank=True, related_name="transactions")
    people = models.ManyToManyField(Person, blank=True, related_name="transactions")
    documents = models.ManyToManyField(Document, blank=True, related_name="transactions")
    reviewed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-booking_date", "-id"]

    def __str__(self):
        return f"{self.booking_date}: {self.description[:40]} ({self.amount} {self.currency})"
