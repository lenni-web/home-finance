import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pypdf import PdfReader

from .models import Document


DATE_RE = re.compile(r"\b([0-3]?\d[./-][01]?\d[./-](?:20)?\d{2})\b")
TEXTUAL_DATE_RE = re.compile(
    r"\b([0-3]?\d)\.?\s+"
    r"(januar|january|februar|february|märz|maerz|march|april|mai|may|juni|june|"
    r"juli|july|august|september|oktober|october|november|dezember|december)"
    r"\s+(20\d{2})\b",
    re.I,
)
AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})(?:\s*(?:EUR|€))?", re.I)
TOTAL_HINT_PRIORITIES = (
    (100, re.compile(r"\b(?:zu zahlender betrag|zahlbetrag|gesamtbetrag|rechnungsbetrag|endbetrag)\b", re.I)),
    (80, re.compile(r"\bzu zahlen\b", re.I)),
    (60, re.compile(r"\b(?:gesamt|summe|total)\b", re.I)),
)
INVOICE_NUMBER_RE = re.compile(
    r"(?:rechnungs(?:nummer|nr\.?|[- ]?nr\.?|[- ]?no\.?)|invoice(?: number| no\.?)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
    re.I,
)
REVERSE_INVOICE_NUMBER_RE = re.compile(
    r"\b([A-Z0-9][A-Z0-9./_-]{2,})[ \t]*rechnungs(?:nummer|nr\.?)\b",
    re.I,
)
SELLER_RE = re.compile(r"\bverkauft\s+von\s+([^\r\n]{2,160})", re.I)
BILLER_LABEL_RE = re.compile(
    r"^(?:in\s+rechnung\s+gestellt\s+von|rechnungssteller|"
    r"billed\s+by|invoice\s+from)\s*:?\s*(.*)$",
    re.I,
)
COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:llc|ltd\.?|limited|inc\.?|corp\.?|corporation|gmbh|ug|ag|se|kg|ohg|"
    r"gbr|s\.?a\.?r\.?l\.?|s\.?r\.?l\.?)\b",
    re.I,
)
BILLER_VALUE_EXCLUDES = (
    "versandkosten", "umsatzsteuer", "zwischensumme", "gesamtsumme", "gesamt", "summe",
    "zahlung", "saldo", "preis", "anzahl", "artikel", "bestellung",
)
MERCHANT_EXCLUDES = (
    "rechnung", "kassenbon", "quittung", "datum", "seite", "kunden", "beleg", "steuer",
    "ust-id", "iban", "betrag", "alle bestellungen anzeigen", "bestellungsübersicht",
)
RECEIPT_MERCHANTS = {
    "aldi": "ALDI",
    "lidl": "Lidl",
    "rewe": "REWE",
    "edeka": "EDEKA",
    "penny": "PENNY",
    "netto": "Netto Marken-Discount",
    "kaufland": "Kaufland",
    "dm drogerie": "dm-drogerie markt",
    "rossmann": "ROSSMANN",
}
KNOWN_MERCHANTS = {
    "congstar": "congstar",
    "amazon": "Amazon",
}
OCR_RECEIPT_HINTS = (
    "zu zahlen", "kartenzahlung", "kundenbeleg", "mwst", "eur", "datum",
    "gesamt", "summe", "terminal", "ust", "beleg",
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
        with tempfile.TemporaryDirectory(prefix="home-finance-image-ocr-") as temp_dir:
            variants = _prepare_receipt_image_variants(path, Path(temp_dir))
            results = []
            for variant, page_segmentation_mode in variants:
                result = subprocess.run(
                    [
                        "tesseract", str(variant), "stdout", "-l", "deu+eng",
                        "--psm", str(page_segmentation_mode), "--dpi", "300",
                        "-c", "preserve_interword_spaces=1",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
                text = result.stdout.decode("utf-8", errors="replace").strip()
                results.append((_ocr_receipt_quality(text), text))
            return max(results, key=lambda item: item[0])[1] if results else ""
    raise ValueError("Nicht unterstütztes Dokumentformat.")


def _otsu_threshold(image):
    histogram = image.histogram()
    total = sum(histogram)
    weighted_sum = sum(index * count for index, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1
    threshold = 180
    for index, count in enumerate(histogram):
        background_weight += count
        if not background_weight:
            continue
        foreground_weight = total - background_weight
        if not foreground_weight:
            break
        background_sum += index * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_sum - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = index
    return threshold


def _prepare_receipt_image_variants(path, target_dir):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("L")
        max_dimension = 3600
        scale = min(2.0, max_dimension / max(image.size))
        if scale != 1:
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.Resampling.LANCZOS,
            )
        image = ImageOps.autocontrast(image, cutoff=(1, 1))
        image = ImageEnhance.Contrast(image).enhance(1.35)
        sharpened = image.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=3))
        threshold = _otsu_threshold(sharpened)
        binary = sharpened.point(lambda value: 255 if value > threshold else 0)
        grayscale_path = target_dir / "receipt-grayscale.png"
        binary_path = target_dir / "receipt-binary.png"
        sharpened.save(grayscale_path, dpi=(300, 300))
        binary.save(binary_path, dpi=(300, 300))
    return [(grayscale_path, 4), (binary_path, 6)]


def _ocr_receipt_quality(text):
    lowered = text.casefold()
    hint_score = sum(20 for hint in OCR_RECEIPT_HINTS if hint in lowered)
    amount_score = min(len(AMOUNT_RE.findall(text)), 30) * 2
    date_score = 20 if DATE_RE.search(text) else 0
    readable_lines = sum(
        1 for line in text.splitlines()
        if len(line.strip()) >= 4 and sum(character.isalnum() for character in line) >= 3
    )
    return hint_score + amount_score + date_score + min(readable_lines, 60)


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
    textual_candidates = list(TEXTUAL_DATE_RE.finditer(text))
    invoice_date_position = text.casefold().find("rechnungsdatum")
    if invoice_date_position >= 0:
        contextual = [
            match for match in textual_candidates
            if invoice_date_position <= match.start() <= invoice_date_position + 120
        ]
        textual_candidates = contextual + [
            match for match in textual_candidates if match not in contextual
        ]
    month_numbers = {
        "januar": 1, "january": 1, "februar": 2, "february": 2,
        "märz": 3, "maerz": 3, "march": 3, "april": 4, "mai": 5, "may": 5,
        "juni": 6, "june": 6, "juli": 7, "july": 7, "august": 8,
        "september": 9, "oktober": 10, "october": 10, "november": 11,
        "dezember": 12, "december": 12,
    }
    for match in textual_candidates:
        parsed = date(
            int(match.group(3)), month_numbers[match.group(2).casefold()], int(match.group(1))
        )
        if date(2000, 1, 1) <= parsed <= timezone.localdate():
            return parsed
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
        priority = max(
            (score for score, pattern in TOTAL_HINT_PRIORITIES if pattern.search(line)),
            default=0,
        )
        if priority and amounts:
            hinted.append((priority, amounts[-1]))
    if hinted:
        highest_priority = max(priority for priority, _amount in hinted)
        return next(amount for priority, amount in hinted if priority == highest_priority)
    return max(all_amounts) if all_amounts else None


def _parse_merchant(text: str) -> str:
    normalized_text = " ".join(text.casefold().split())
    seller_match = SELLER_RE.search(text)
    if seller_match:
        seller = " ".join(seller_match.group(1).split()).strip(" -|:,. ")
        if seller:
            return seller
    lines = text.splitlines()
    for index, line in enumerate(lines):
        biller_match = BILLER_LABEL_RE.match(" ".join(line.split()))
        if not biller_match:
            continue
        candidates = [biller_match.group(1), *lines[index + 1:index + 7]]
        cleaned_candidates = []
        for value in candidates:
            biller = " ".join(value.split()).strip(" -|:,. ")
            lowered = biller.casefold()
            if not biller or not any(character.isalpha() for character in biller):
                continue
            if any(excluded in lowered for excluded in BILLER_VALUE_EXCLUDES):
                continue
            cleaned_candidates.append(biller)
        company_candidate = next(
            (candidate for candidate in cleaned_candidates if COMPANY_SUFFIX_RE.search(candidate)),
            None,
        )
        if company_candidate:
            return company_candidate
        if cleaned_candidates:
            return cleaned_candidates[0]
    for needle, merchant in KNOWN_MERCHANTS.items():
        if needle in normalized_text:
            return merchant
    document_lead = " ".join(text.casefold().splitlines()[:30])
    for needle, merchant in RECEIPT_MERCHANTS.items():
        if needle in document_lead:
            return merchant
    for line in lines[:15]:
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
        if re.fullmatch(r"[A-Z]{2,}(?:-[A-Z0-9]{2,})+", candidate):
            continue
        return candidate
    return ""


def _parse_invoice_number(text: str) -> str:
    reverse_match = REVERSE_INVOICE_NUMBER_RE.search(text)
    if reverse_match:
        return reverse_match.group(1).strip(".,")
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
