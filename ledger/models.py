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
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Tag(TimestampedModel):
    name = models.CharField(max_length=80, unique=True)
    color = models.CharField(max_length=7, default="#0f766e")
    active = models.BooleanField(default=True)

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

    class ProcessingStatus(models.TextChoices):
        PENDING = "pending", "Ausstehend"
        PROCESSING = "processing", "Wird verarbeitet"
        REVIEW = "review", "Prüfung erforderlich"
        PROCESSED = "processed", "Verarbeitet"
        FAILED = "failed", "Fehlgeschlagen"

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
    processing_status = models.CharField(
        max_length=20, choices=ProcessingStatus.choices, default=ProcessingStatus.PENDING
    )
    processing_error = models.TextField(blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
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

    class ReconciliationStatus(models.TextChoices):
        UNAVAILABLE = "unavailable", "Nicht prüfbar"
        BALANCED = "balanced", "Ausgeglichen"
        MISMATCH = "mismatch", "Abweichung"

    document = models.OneToOneField(Document, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    parser_name = models.CharField(max_length=100, blank=True)
    parser_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED)
    error_message = models.TextField(blank=True)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    closing_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    transaction_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    reconciliation_difference = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    reconciliation_status = models.CharField(
        max_length=20,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.UNAVAILABLE,
    )


class Transaction(TimestampedModel):
    statement_import = models.ForeignKey(
        StatementImport, on_delete=models.PROTECT, related_name="transactions"
    )
    booking_date = models.DateField(db_index=True)
    value_date = models.DateField(null=True, blank=True)
    booking_type = models.CharField(max_length=120, blank=True)
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


class CategorizationRule(TimestampedModel):
    name = models.CharField(max_length=160)
    match_text = models.CharField(max_length=255, unique=True)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL)
    tags = models.ManyToManyField(Tag, blank=True, related_name="categorization_rules")
    people = models.ManyToManyField(Person, blank=True, related_name="categorization_rules")
    auto_apply = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    times_applied = models.PositiveIntegerField(default=0)
    priority = models.PositiveIntegerField(
        default=100,
        help_text="Höhere Werte werden zuerst ausgewertet.",
    )

    class Meta:
        ordering = ["-priority", "name"]

    def matches(self, transaction):
        from .rules import normalize_merchant

        needle = normalize_merchant(self.match_text)
        haystack = normalize_merchant(f"{transaction.counterparty} {transaction.description}")
        return bool(needle) and needle in haystack

    def __str__(self):
        return self.name


class EmailImportConfig(TimestampedModel):
    class Security(models.TextChoices):
        SSL = "ssl", "SSL/TLS"
        STARTTLS = "starttls", "STARTTLS"

    enabled = models.BooleanField(default=False)
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(default=993)
    security = models.CharField(max_length=20, choices=Security.choices, default=Security.SSL)
    username = models.CharField(max_length=255, blank=True)
    encrypted_password = models.TextField(blank=True, editable=False)
    folder = models.CharField(max_length=255, default="INBOX")
    allowed_senders = models.TextField(
        blank=True,
        help_text="Eine Adresse pro Zeile. Leer bedeutet: alle Absender akzeptieren.",
    )
    poll_interval_minutes = models.PositiveIntegerField(default=5)
    mark_as_read = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def set_password(self, password):
        from .email_import import encrypt_password
        self.encrypted_password = encrypt_password(password) if password else ""

    def get_password(self):
        from .email_import import decrypt_password
        return decrypt_password(self.encrypted_password) if self.encrypted_password else ""

    def __str__(self):
        return self.username or "E-Mail-Import"


class EmailImportMessage(TimestampedModel):
    config = models.ForeignKey(EmailImportConfig, on_delete=models.CASCADE)
    mailbox_uid = models.CharField(max_length=255)
    message_id = models.CharField(max_length=998, blank=True)
    sender = models.CharField(max_length=320, blank=True)
    subject = models.CharField(max_length=998, blank=True)
    attachment_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["config", "mailbox_uid"], name="unique_email_import_uid"
        )]
        ordering = ["-created_at"]
