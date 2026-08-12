import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone
from pypdf import PdfReader

from .models import Document


DATE_RE = re.compile(r"\b([0-3]?\d[./-][01]?\d[./-](?:20)?\d{2})\b")
AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})(?:\s*(?:EUR|€))?", re.I)
TOTAL_HINTS = ("gesamt", "summe", "total", "zu zahlen", "rechnungsbetrag", "endbetrag")
INVOICE_NUMBER_RE = re.compile(
    r"(?:rechnungs(?:nummer|nr\.?|[- ]?nr\.?|[- ]?no\.?)|invoice(?: number| no\.?)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
    re.I,
)
MERCHANT_EXCLUDES = (
    "rechnung", "kassenbon", "quittung", "datum", "seite", "kunden", "beleg", "steuer",
    "ust-id", "iban", "betrag",
)


@dataclass(frozen=True)
class DocumentExtraction:
    text: str
    document_date: date | None
    merchant: str
    total_amount: Decimal | None
    invoice_number: str
    confidence: dict


def extract_document_text(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        reader = PdfReader(path)
        direct_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if len(direct_text) >= 40:
            return direct_text
        with tempfile.TemporaryDirectory(prefix="home-finance-ocr-") as temp_dir:
            sidecar = Path(temp_dir) / "ocr.txt"
            output_pdf = Path(temp_dir) / "searchable.pdf"
            subprocess.run(
                [
                    "ocrmypdf", "--skip-text", "--sidecar", str(sidecar), "-l", "deu+eng",
                    str(path), str(output_pdf),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            return sidecar.read_text(encoding="utf-8", errors="replace").strip()
    if suffix in {".jpg", ".jpeg", ".png"}:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "deu+eng"],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return result.stdout.decode("utf-8", errors="replace").strip()
    raise ValueError("Nicht unterstütztes Dokumentformat.")


def _parse_date(text: str) -> date | None:
    candidates = DATE_RE.findall(text)
    for candidate in candidates:
        normalized = candidate.replace("/", ".").replace("-", ".")
        for format_string in ("%d.%m.%Y", "%d.%m.%y"):
            try:
                parsed = datetime.strptime(normalized, format_string).date()
                if date(2000, 1, 1) <= parsed <= timezone.localdate():
                    return parsed
            except ValueError:
                pass
    return None


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _parse_total(text: str) -> Decimal | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    hinted = []
    all_amounts = []
    for line in lines:
        amounts = [_decimal(value) for value in AMOUNT_RE.findall(line)]
        amounts = [value for value in amounts if value is not None and value >= 0]
        all_amounts.extend(amounts)
        if any(hint in line.casefold() for hint in TOTAL_HINTS):
            hinted.extend(amounts)
    candidates = hinted or all_amounts
    return candidates[-1] if hinted else (max(candidates) if candidates else None)


def _parse_merchant(text: str) -> str:
    for line in text.splitlines()[:15]:
        candidate = " ".join(line.split()).strip(" -|:")
        lowered = candidate.casefold()
        if not (3 <= len(candidate) <= 120):
            continue
        if not any(character.isalpha() for character in candidate):
            continue
        if any(excluded in lowered for excluded in MERCHANT_EXCLUDES):
            continue
        if AMOUNT_RE.fullmatch(candidate):
            continue
        return candidate
    return ""


def _parse_invoice_number(text: str) -> str:
    match = INVOICE_NUMBER_RE.search(text)
    return match.group(1).strip(".,") if match else ""


def analyze_document(path: Path) -> DocumentExtraction:
    text = extract_document_text(path)
    document_date = _parse_date(text)
    merchant = _parse_merchant(text)
    total_amount = _parse_total(text)
    invoice_number = _parse_invoice_number(text)
    return DocumentExtraction(
        text=text,
        document_date=document_date,
        merchant=merchant,
        total_amount=total_amount,
        invoice_number=invoice_number,
        confidence={
            "document_date": 90 if document_date else 0,
            "merchant": 75 if merchant else 0,
            "total_amount": 90 if total_amount is not None else 0,
            "invoice_number": 90 if invoice_number else 0,
        },
    )


def process_document(document: Document) -> DocumentExtraction:
    document.processing_status = Document.ProcessingStatus.PROCESSING
    document.processing_error = ""
    document.save(update_fields=["processing_status", "processing_error", "updated_at"])
    try:
        result = analyze_document(Path(document.file.path))
        document.extracted_text = result.text
        if not document.document_date:
            document.document_date = result.document_date
        if not document.merchant:
            document.merchant = result.merchant
        if not document.invoice_number:
            document.invoice_number = result.invoice_number
        if document.total_amount is None:
            document.total_amount = result.total_amount
        if not document.title:
            document.title = result.merchant or document.original_filename
        document.processing_status = Document.ProcessingStatus.REVIEW
        document.extraction_confidence = result.confidence
        document.extracted_at = timezone.now()
        document.save(update_fields=[
            "extracted_text", "document_date", "merchant", "invoice_number", "total_amount", "title",
            "extraction_confidence",
            "processing_status", "extracted_at", "updated_at",
        ])
        return result
    except Exception as exc:
        document.processing_status = Document.ProcessingStatus.FAILED
        document.processing_error = str(exc)
        document.save(update_fields=["processing_status", "processing_error", "updated_at"])
        raise
