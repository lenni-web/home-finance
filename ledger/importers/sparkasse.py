import re
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

from .base import ParsedStatement, ParsedTransaction
from .ing import TextFragment

DATE_ROW_RE = re.compile(r"^\s*(\d{2}\.\d{2}\.(?:\d{4}|\d{2}))\s*(.*?)\s*$", re.S)
_AMOUNT = r"((?:\d{1,3}(?:\.\d{3})*|\d+),\d{2})"
AMOUNT_RE = re.compile(rf"^\s*([+-]?){_AMOUNT}\s*([+-]|[SH])?\s*$")
BALANCE_TAIL_RE = re.compile(rf"(?:^|\s)([+-]?){_AMOUNT}\s*([+-]|[SH])?\s*$")

# Spaltenlage des Sparkassen-Kontoauszugs (PDF-Punkte)
DATE_COLUMN = (55, 85)
DESCRIPTION_COLUMN = (100, 480)
BALANCE_COLUMN = (100, 150)
AMOUNT_COLUMN_START = 440
FOOTER_Y = 60


def _signed(sign, digits, marker):
    value = Decimal(digits.replace(".", "").replace(",", "."))
    negative = sign == "-" or marker in {"-", "S"}
    return -value if negative else value


def _normalize_date(value):
    day, month, year = value.rstrip(".").split(".")
    year = f"20{year}" if len(year) == 2 else year
    return f"{year}-{month}-{day}"


class SparkasseStatementParser:
    """Parser für den PDF-Kontoauszug der Sparkassen (Spalten: Datum, Erläuterung, Betrag).

    Eine Buchung besteht aus einer Zeile links (Datum und Buchungsart), den
    Erläuterungszeilen darunter und dem Betrag rechts auf Höhe der Datumszeile. Die
    Kontostände stehen als ``Kontostand … <Betrag>`` vor der ersten und nach der letzten
    Buchung. ``Übertrag:`` am Seitenende wird ignoriert.

    Die erste Erläuterungszeile gilt als Zahlungspartner, die übrigen als Verwendungszweck.
    Das Buchungsdatum dient auch als Wertstellung, weil der Auszug nur ein Datum je Buchung
    ausweist.
    """

    name = "sparkasse_pdf"
    version = 1
    label = "Sparkasse (PDF)"
    file_extension = ".pdf"

    @staticmethod
    def _lines(fragments):
        ordered = sorted(fragments, key=lambda item: (-item.y, item.x))
        lines = (re.sub(r"\s+", " ", item.text).strip() for item in ordered)
        return [line for line in lines if line]

    def _parse_page(self, fragments):
        """Liefert (Buchungen, Kontostandzeilen) einer Seite in Lesereihenfolge."""
        header_y = next((
            item.y for item in fragments
            if item.text.strip().startswith("Datum") and "Betrag" in item.text
        ), None)
        if header_y is None:
            return [], []
        body = [
            item for item in fragments
            if item.text.strip() and FOOTER_Y < item.y < header_y - 1
        ]

        rows = sorted(
            (
                item for item in body
                if DATE_COLUMN[0] <= item.x <= DATE_COLUMN[1] and DATE_ROW_RE.match(item.text)
            ),
            key=lambda item: -item.y,
        )
        balances = []
        boundaries = []
        for item in body:
            text = item.text.strip()
            if text.startswith("Übertrag"):
                boundaries.append(item.y)
            elif (
                text.startswith("Kontostand")
                and BALANCE_COLUMN[0] <= item.x <= BALANCE_COLUMN[1]
            ):
                match = BALANCE_TAIL_RE.search(text)
                if match:
                    balances.append((item.y, _signed(*match.groups())))
                    boundaries.append(item.y)

        amounts = [
            item for item in body
            if item.x >= AMOUNT_COLUMN_START and AMOUNT_RE.match(item.text)
        ]
        transactions = []
        for index, row in enumerate(rows):
            lower = rows[index + 1].y if index + 1 < len(rows) else max(
                (y for y in boundaries if y < row.y), default=FOOTER_Y
            )
            amount_fragment = next((a for a in amounts if abs(a.y - row.y) < 2), None)
            if amount_fragment is None:
                continue  # fällt in der Saldenprüfung als Abweichung auf
            amount = _signed(*AMOUNT_RE.match(amount_fragment.text).groups())
            date_text, booking_type = DATE_ROW_RE.match(row.text).groups()
            detail_lines = self._lines(
                item for item in body
                if DESCRIPTION_COLUMN[0] <= item.x < DESCRIPTION_COLUMN[1]
                and lower < item.y < row.y - 1
                and not item.text.strip().startswith(("Übertrag", "Kontostand"))
            )
            date = _normalize_date(date_text)
            transactions.append(ParsedTransaction(
                booking_date=date,
                value_date=date,
                booking_type=booking_type,
                counterparty=detail_lines[0] if detail_lines else "",
                description="\n".join(detail_lines[1:]),
                amount=str(amount.quantize(Decimal("0.01"))),
                currency="EUR",
                source_page=row.page,
            ))
        balance_lines = [
            (item_y, value, "before" if rows and item_y > rows[0].y else "after")
            for item_y, value in sorted(balances, reverse=True)
        ]
        return transactions, balance_lines

    def parse_fragments(self, pages: list[list[TextFragment]]) -> ParsedStatement:
        transactions = []
        opening = closing = None
        for fragments in pages:
            page_transactions, balances = self._parse_page(fragments)
            for _y, value, position in balances:
                if position == "before":
                    if opening is None and not transactions:
                        opening = value  # Anfangskontostand steht vor der ersten Buchung
                else:
                    closing = value  # der letzte Kontostand nach einer Buchung gilt
            transactions.extend(page_transactions)
        return ParsedStatement(
            transactions=transactions, opening_balance=opening, closing_balance=closing
        )

    def parse_statement(self, path: Path) -> ParsedStatement:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Der Kontoauszug ist verschlüsselt.")
        pages = []
        for page_number, page in enumerate(reader.pages, start=1):
            fragments = []

            def visit_text(text, _cm, tm, _font_dictionary, _font_size, number=page_number):
                if text.strip():
                    fragments.append(TextFragment(
                        page=number, x=round(float(tm[4]), 2), y=round(float(tm[5]), 2), text=text,
                    ))

            page.extract_text(visitor_text=visit_text)
            pages.append(fragments)
        return self.parse_fragments(pages)
