import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader


DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
AMOUNT_RE = re.compile(r"^-?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2}$")


@dataclass(frozen=True)
class TextFragment:
    page: int
    x: float
    y: float
    text: str


@dataclass(frozen=True)
class ParsedTransaction:
    booking_date: str
    value_date: str
    booking_type: str
    counterparty: str
    description: str
    amount: str
    currency: str
    source_page: int

    def to_dict(self):
        return asdict(self)


class INGStatementParser:
    """Parser for the current ING Germany statement layout.

    ING's PDF text stream places the two transaction dates in the left column
    (approximately x=70.8). Other fragments may report 0/0 coordinates, so the
    parser intentionally combines the date-column position with text order.
    """

    name = "ing_de_pdf"
    version = 1

    @staticmethod
    def _date(value: str) -> str:
        parsed = datetime.strptime(value, "%d.%m.%Y").date()
        return parsed.isoformat()

    @staticmethod
    def _amount(value: str) -> str:
        normalized = value.replace(".", "").replace(",", ".")
        return str(Decimal(normalized).quantize(Decimal("0.01")))

    @staticmethod
    def _is_transaction_date(fragment: TextFragment) -> bool:
        value = fragment.text.strip()
        return 45 <= fragment.x <= 110 and DATE_RE.fullmatch(value) is not None

    @staticmethod
    def _is_page_chrome(fragment: TextFragment) -> bool:
        value = fragment.text.strip()
        if fragment.x > 0 and fragment.y <= 80:
            return True
        if fragment.x > 0 and value in {
            "Alter Saldo", "Neuer Saldo", "Eingeräumte Kontoüberziehung",
        }:
            return True
        if fragment.x >= 300 and value in {"Datum", "Seite", "IBAN", "BIC"}:
            return True
        return value.startswith(("Girokonto Nummer", "Kontoauszug "))

    @staticmethod
    def _clean(parts: list[str]) -> list[str]:
        return [part.strip() for part in parts if part.strip()]

    def parse_fragments(self, pages: list[list[TextFragment]]) -> list[ParsedTransaction]:
        parsed: list[ParsedTransaction] = []
        for fragments in pages:
            current: dict | None = None
            for fragment in fragments:
                value = fragment.text.strip()
                if not value:
                    continue

                if self._is_transaction_date(fragment):
                    if current is None:
                        current = {
                            "booking_date": value,
                            "page": fragment.page,
                            "before_amount": [],
                            "after_value_date": [],
                            "amount": None,
                            "value_date": None,
                        }
                    elif current["amount"] is not None and current["value_date"] is None:
                        current["value_date"] = value
                    elif current["value_date"] is not None:
                        transaction = self._finish(current)
                        if transaction:
                            parsed.append(transaction)
                        current = {
                            "booking_date": value,
                            "page": fragment.page,
                            "before_amount": [],
                            "after_value_date": [],
                            "amount": None,
                            "value_date": None,
                        }
                    continue

                if current is None:
                    continue
                if current["value_date"] is not None and self._is_page_chrome(fragment):
                    transaction = self._finish(current)
                    if transaction:
                        parsed.append(transaction)
                    current = None
                    break
                if current["amount"] is None and AMOUNT_RE.fullmatch(value):
                    current["amount"] = value
                elif current["value_date"] is None:
                    current["before_amount"].append(value)
                else:
                    current["after_value_date"].append(value)

            if current:
                transaction = self._finish(current)
                if transaction:
                    parsed.append(transaction)
        return parsed

    def _finish(self, current: dict) -> ParsedTransaction | None:
        if current["amount"] is None or current["value_date"] is None:
            return None
        leading = self._clean(current["before_amount"])
        details = self._clean(current["after_value_date"])
        booking_type = leading[0] if leading else ""
        counterparty = leading[1] if len(leading) > 1 else ""
        description = "\n".join(leading[2:] + details)
        return ParsedTransaction(
            booking_date=self._date(current["booking_date"]),
            value_date=self._date(current["value_date"]),
            booking_type=booking_type,
            counterparty=counterparty,
            description=description,
            amount=self._amount(current["amount"]),
            currency="EUR",
            source_page=current["page"],
        )

    def parse_fixture(self, fixture_path: Path) -> list[ParsedTransaction]:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        pages = [[TextFragment(
            page=int(item["page"]),
            x=float(item["x"]),
            y=float(item["y"]),
            text=str(item["text"]),
        ) for item in page["fragments"]] for page in data["pages"]]
        return self.parse_fragments(pages)

    def parse_pdf(self, pdf_path: Path) -> list[ParsedTransaction]:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            raise ValueError("Der Kontoauszug ist verschlüsselt.")
        pages: list[list[TextFragment]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            fragments: list[TextFragment] = []

            def visit_text(text, _cm, tm, _font_dictionary, _font_size):
                if text.strip():
                    fragments.append(TextFragment(
                        page=page_number,
                        x=round(float(tm[4]), 2),
                        y=round(float(tm[5]), 2),
                        text=text,
                    ))

            page.extract_text(visitor_text=visit_text)
            pages.append(fragments)
        return self.parse_fragments(pages)
