import re
import subprocess
import tempfile
import csv
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
    r"(januar|january|jan|februar|february|feb|märz|maerz|march|mar|april|apr|"
    r"mai|may|juni|june|jun|juli|july|jul|august|aug|september|sept?|oktober|"
    r"october|oct|november|nov|dezember|december|dec)"
    r",?\s+(20\d{2})\b",
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
    r"\b(?:LLC|L\.L\.C\.|Ltd\.?|LTD\.?|Limited|Inc\.?|INC\.?|Corp\.?|"
    r"Corporation|GmbH|UG|AG|SE|KG|OHG|GbR|S\.?A\.?R\.?L\.?|S\.?r\.?l\.?)"
    r"(?=[\s,.;)]|$)",
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


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    line_key: tuple[int, int, int, int]


@dataclass(frozen=True)
class ExtractedContent:
    text: str
    method: str
    variant: str = ""
    words: tuple[OCRWord, ...] = ()


def _read_tesseract_tsv(path: Path) -> tuple[OCRWord, ...]:
    words = []
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            value = (row.get("text") or "").strip()
            if not value:
                continue
            try:
                confidence = float(row.get("conf", "-1"))
                words.append(OCRWord(
                    text=value,
                    confidence=confidence,
                    left=int(row["left"]), top=int(row["top"]),
                    width=int(row["width"]), height=int(row["height"]),
                    line_key=(
                        int(row["page_num"]), int(row["block_num"]),
                        int(row["par_num"]), int(row["line_num"]),
                    ),
                ))
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(words)


def extract_document_content(path: Path) -> ExtractedContent:
    """Extract text plus layout metadata where OCR can provide it."""
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        reader = PdfReader(path)
        direct_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if len(direct_text) >= 40:
            return ExtractedContent(direct_text, "pdf-text")
        with tempfile.TemporaryDirectory(prefix="home-finance-ocr-") as temp_dir:
            sidecar = Path(temp_dir) / "ocr.txt"
            output_pdf = Path(temp_dir) / "searchable.pdf"
            subprocess.run(
                [
                    "ocrmypdf", "--skip-text", "--rotate-pages", "--deskew", "--clean",
                    "--sidecar", str(sidecar), "-l", "deu+eng", str(path), str(output_pdf),
                ],
                check=True, capture_output=True, timeout=300,
            )
            return ExtractedContent(
                sidecar.read_text(encoding="utf-8", errors="replace").strip(), "pdf-ocr",
                "rotate+deskew+clean",
            )
    if suffix in {".jpg", ".jpeg", ".png"}:
        with tempfile.TemporaryDirectory(prefix="home-finance-image-ocr-") as temp_dir:
            variants = _prepare_receipt_image_variants(path, Path(temp_dir))
            results = []
            for variant, page_segmentation_mode in variants:
                output_base = Path(temp_dir) / f"ocr-{variant.stem}-psm{page_segmentation_mode}"
                subprocess.run(
                    [
                        "tesseract", str(variant), str(output_base), "-l", "deu+eng",
                        "--psm", str(page_segmentation_mode), "--dpi", "300",
                        "-c", "preserve_interword_spaces=1", "txt", "tsv",
                    ],
                    check=True, capture_output=True, timeout=180,
                )
                text = output_base.with_suffix(".txt").read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                words = _read_tesseract_tsv(output_base.with_suffix(".tsv"))
                mean_confidence = (
                    sum(word.confidence for word in words if word.confidence >= 0)
                    / max(sum(1 for word in words if word.confidence >= 0), 1)
                )
                score = _ocr_receipt_quality(text) + min(mean_confidence, 100) / 5
                results.append((score, ExtractedContent(
                    text, "image-ocr", f"{variant.stem}/psm-{page_segmentation_mode}", words,
                )))
            return max(results, key=lambda item: item[0])[1] if results else ExtractedContent("", "image-ocr")
    raise ValueError("Nicht unterstütztes Dokumentformat.")


def extract_document_text(path: Path) -> str:
    return extract_document_content(path).text


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
    # PSM 4/6 work well for receipts; sparse PSM 11 often wins on portal screenshots
    # and modern invoices with widely separated layout blocks.
    return [(grayscale_path, 4), (binary_path, 6), (grayscale_path, 11)]


def _ocr_receipt_quality(text):
    lowered = text.casefold()
    hint_score = sum(20 for hint in OCR_RECEIPT_HINTS if hint in lowered)
    merchant_score = 80 if any(merchant in lowered for merchant in RECEIPT_MERCHANTS) else 0
    amount_score = min(len(AMOUNT_RE.findall(text)), 30) * 2
    date_score = 20 if DATE_RE.search(text) else 0
    readable_lines = sum(
        1 for line in text.splitlines()
        if len(line.strip()) >= 4 and sum(character.isalnum() for character in line) >= 3
    )
    return hint_score + merchant_score + amount_score + date_score + min(readable_lines, 60)


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
        "januar": 1, "january": 1, "jan": 1,
        "februar": 2, "february": 2, "feb": 2,
        "märz": 3, "maerz": 3, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "mai": 5, "may": 5,
        "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "oktober": 10, "october": 10, "oct": 10, "november": 11, "nov": 11,
        "dezember": 12, "december": 12, "dec": 12,
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
    document_lines = text.casefold().splitlines()
    document_perimeter = " ".join(document_lines[:30] + document_lines[-30:])
    for needle, merchant in RECEIPT_MERCHANTS.items():
        if needle in document_perimeter:
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


def _layout_lines(words: tuple[OCRWord, ...]):
    grouped = {}
    for word in words:
        grouped.setdefault(word.line_key, []).append(word)
    lines = []
    for line_words in grouped.values():
        line_words.sort(key=lambda word: word.left)
        lines.append({
            "text": " ".join(word.text for word in line_words),
            "left": min(word.left for word in line_words),
            "top": min(word.top for word in line_words),
            "bottom": max(word.top + word.height for word in line_words),
            "confidence": round(sum(word.confidence for word in line_words) / len(line_words)),
        })
    return sorted(lines, key=lambda line: (line["top"], line["left"]))


def _layout_merchant_candidate(content: ExtractedContent):
    """Use OCR coordinates to distinguish seller blocks from adjacent columns."""
    lines = _layout_lines(content.words)
    if not lines:
        return None
    labels = [
        line for line in lines
        if BILLER_LABEL_RE.match(line["text"]) or "verkauft von" in line["text"].casefold()
    ]
    recipient_labels = [
        line for line in lines
        if any(label in line["text"].casefold() for label in ("rechnungsempfänger", "bill to", "sold to"))
    ]
    candidates = []
    for line in lines:
        candidate = " ".join(line["text"].split()).strip(" -|:,. ")
        lowered = candidate.casefold()
        if not COMPANY_SUFFIX_RE.search(candidate):
            continue
        if any(excluded in lowered for excluded in MERCHANT_EXCLUDES + BILLER_VALUE_EXCLUDES):
            continue
        score = 68 + min(max(line["confidence"], 0), 100) // 10
        reasons = ["Firmenname mit Rechtsform im OCR-Layout"]
        for label in labels:
            vertical_distance = line["top"] - label["bottom"]
            horizontal_distance = abs(line["left"] - label["left"])
            if -20 <= vertical_distance <= 500 and horizontal_distance <= 800:
                score += 22
                reasons.append("räumlich beim Feld für den Rechnungssteller")
                break
        for label in recipient_labels:
            if 0 <= line["top"] - label["bottom"] <= 350 and abs(line["left"] - label["left"]) <= 300:
                score -= 35
                reasons.append("möglicherweise im Empfängerblock")
                break
        candidates.append((score, candidate, reasons))
    return max(candidates, default=None, key=lambda item: item[0])


def _merchant_analysis(content: ExtractedContent):
    merchant = _parse_merchant(content.text)
    lowered = content.text.casefold()
    score = 0
    reasons = []
    if merchant:
        score = 58
        reasons.append("plausible Textzeile im Dokumentkopf")
        if SELLER_RE.search(content.text) or any(
            BILLER_LABEL_RE.match(" ".join(line.split())) for line in content.text.splitlines()
        ):
            score = 94
            reasons = ["eindeutiges Feld für Verkäufer oder Rechnungssteller"]
        elif any(needle in lowered for needle in KNOWN_MERCHANTS):
            score = 88
            reasons = ["bekannter Anbieter im Dokumenttext"]
        elif merchant in RECEIPT_MERCHANTS.values():
            score = 90
            reasons = ["bekannter Händler im Kopf oder Fuß des Kassenbelegs"]
        elif COMPANY_SUFFIX_RE.search(merchant):
            score = 82
            reasons = ["Firmenname mit erkannter Rechtsform"]
    layout = _layout_merchant_candidate(content)
    if layout and layout[0] > score:
        score, merchant, reasons = layout
    return merchant, min(score, 99), reasons


def _layout_total_candidate(content: ExtractedContent):
    lines = _layout_lines(content.words)
    labels = []
    amount_lines = []
    for line in lines:
        priority = max(
            (score for score, pattern in TOTAL_HINT_PRIORITIES if pattern.search(line["text"])),
            default=0,
        )
        if priority:
            labels.append((priority, line))
        amounts = [_decimal(value) for value in AMOUNT_RE.findall(line["text"])]
        for amount in (value for value in amounts if value is not None and value >= 0):
            amount_lines.append((amount, line))
    candidates = []
    for priority, label in labels:
        for amount, amount_line in amount_lines:
            vertical_distance = amount_line["top"] - label["bottom"]
            same_row = abs(amount_line["top"] - label["top"]) <= 80
            just_below = -20 <= vertical_distance <= 140
            horizontally_plausible = amount_line["left"] >= label["left"] - 100
            if horizontally_plausible and (same_row or just_below):
                score = min(72 + priority // 4 + max(amount_line["confidence"], 0) // 20, 99)
                candidates.append((score, amount, [
                    f"räumlich dem Summenfeld „{label['text']}“ zugeordnet"
                ]))
    return max(candidates, default=None, key=lambda item: item[0])


def _total_analysis(content: ExtractedContent):
    text = content.text
    amount = _parse_total(text)
    if amount is None:
        layout = _layout_total_candidate(content)
        return (layout[1], layout[0], layout[2]) if layout else (None, 0, [])
    best_priority = 0
    best_label = ""
    for line in text.splitlines():
        if amount not in [_decimal(value) for value in AMOUNT_RE.findall(line)]:
            continue
        for priority, pattern in TOTAL_HINT_PRIORITIES:
            match = pattern.search(line)
            if match and priority > best_priority:
                best_priority, best_label = priority, match.group(0)
    result = (
        amount, min(70 + best_priority // 4, 98), [f"Betrag beim Feld „{best_label}“"]
    ) if best_priority else (amount, 52, ["größter plausibler Betrag; bitte prüfen"])
    layout = _layout_total_candidate(content)
    if layout and layout[0] > result[1]:
        return layout[1], layout[0], layout[2]
    return result


def _date_analysis(text: str):
    parsed = _parse_date(text)
    if not parsed:
        return None, 0, []
    lowered = text.casefold()
    contextual = any(label in lowered for label in ("rechnungsdatum", "invoice date", "belegdatum"))
    return parsed, (94 if contextual else 70), [
        "Datum in einem bezeichneten Datumsfeld" if contextual else "plausibles Datum im Dokument"
    ]


def analyze_document(path: Path) -> DocumentExtraction:
    content = extract_document_content(path)
    text = content.text
    document_date, date_confidence, date_reasons = _date_analysis(text)
    merchant, merchant_confidence, merchant_reasons = _merchant_analysis(content)
    total_amount, amount_confidence, amount_reasons = _total_analysis(content)
    invoice_number = _parse_invoice_number(text)
    return DocumentExtraction(
        text=text,
        document_date=document_date,
        merchant=merchant,
        total_amount=total_amount,
        invoice_number=invoice_number,
        confidence={
            "document_date": date_confidence,
            "merchant": merchant_confidence,
            "total_amount": amount_confidence,
            "invoice_number": 90 if invoice_number else 0,
            "method": content.method,
            "variant": content.variant,
            "details": {
                "document_date": date_reasons,
                "merchant": merchant_reasons,
                "total_amount": amount_reasons,
                "invoice_number": (["eindeutig bezeichnete Rechnungsnummer"] if invoice_number else []),
            },
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
