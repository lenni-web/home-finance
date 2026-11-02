import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..text_normalization import normalize_comparison_text
from .base import ParsedStatement, ParsedTransaction

DELIMITERS = (";", "\t", ",")
DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")

# Kopfzeilen werden mit normalize_comparison_text verglichen (klein, ohne Umlaute).
VALUE_DATE = ("wertstellung", "wertstellungsdatum", "valuta", "valutadatum")
BOOKING_DATE = ("buchungstag", "buchungsdatum", "buchung", "booking date", "datum", "date")
AMOUNT = ("betrag", "umsatz", "amount")
DEBIT = ("soll", "belastung", "ausgang")
CREDIT = ("haben", "gutschrift", "eingang")
# Zahlungsempfänger (bei Ausgaben) und Zahlungspflichtiger (bei Einnahmen) können eigene
# Spalten sein; eine kombinierte Spalte ("Auftraggeber/Empfänger") zählt als Empfänger.
PAYEE = ("beguenstigter", "begunstigter", "empfanger", "zahlungsempfanger", "payee")
PAYER = ("zahlungspflichtiger", "zahlungspflichtige", "auftraggeber", "payer")
STATUS = ("status",)
PENDING = ("vorgemerkt", "pending")
DESCRIPTION = ("verwendungszweck", "beschreibung", "mitteilung", "zweck", "description")
BOOKING_TYPE = ("buchungstext", "umsatztyp", "buchungsart", "transaktionstyp")
BALANCE = ("saldo", "balance")
CURRENCY = ("waehrung", "wahrung", "currency")


def _words(header):
    return normalize_comparison_text(header).split()


def _matches(header, synonyms):
    normalized = normalize_comparison_text(header)
    words = set(normalized.split())
    return any(
        synonym in words or normalized == synonym or normalized.startswith(synonym + " ")
        for synonym in synonyms
    )


def parse_date(value):
    value = (value or "").strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def parse_amount(value):
    """Deutsche (1.234,56) und englische (1,234.56) Schreibweise, auch mit Vorzeichen/Währung."""
    text = re.sub(r"[^\d,.\-+]", "", (value or "").replace("\xa0", " "))
    if not re.search(r"\d", text):
        return None
    negative = text.startswith("-") or text.endswith("-")
    text = text.strip("+-")
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
    elif "," in text:
        decimal_separator = ","
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        decimal_separator = None
    else:
        decimal_separator = "."
    if decimal_separator:
        integer, _, fraction = text.rpartition(decimal_separator)
        text = re.sub(r"[.,]", "", integer) + "." + fraction
    else:
        text = text.replace(".", "")
    try:
        number = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -number if negative else number


class CSVStatementParser:
    """Liest CSV-Umsatzexporte deutscher Banken mit automatischer Spaltenerkennung.

    Erkannt werden Trennzeichen, Zeichensatz, Kopfzeile (auch nach Vorspann), Datums-,
    Betrags- (oder Soll/Haben-), Empfänger-/Zahler-, Zweck- und optionale Saldospalten.
    Vorgemerkte Umsätze werden übersprungen.
    """

    name = "csv_generic"
    version = 1
    label = "CSV-Export (automatische Spaltenerkennung)"
    file_extension = ".csv"

    @staticmethod
    def _decode(raw):
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1")

    def _locate_header(self, text):
        for delimiter in DELIMITERS:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
            for index, row in enumerate(rows):
                columns = self._map_columns(row)
                if columns:
                    return rows, index, columns
        raise ValueError(
            "In der CSV-Datei wurde keine Kopfzeile mit Datums- und Betragsspalte gefunden."
        )

    @staticmethod
    def _map_columns(header):
        """Ordnet Rollen den Spaltenindizes zu; ``None``, wenn Pflichtspalten fehlen."""
        columns = {}
        taken = set()

        def assign(role, synonyms, *, exclude=()):
            for index, cell in enumerate(header):
                if index in taken or not cell.strip():
                    continue
                if _matches(cell, synonyms) and not _matches(cell, exclude):
                    columns[role] = index
                    taken.add(index)
                    return

        assign("value_date", VALUE_DATE)
        assign("booking_date", BOOKING_DATE, exclude=VALUE_DATE)
        assign("amount", AMOUNT)
        if "amount" not in columns:
            assign("debit", DEBIT)
            assign("credit", CREDIT)
        assign("payee", PAYEE)
        assign("payer", PAYER)
        assign("status", STATUS)
        assign("booking_type", BOOKING_TYPE)
        assign("description", DESCRIPTION)
        assign("balance", BALANCE)
        assign("currency", CURRENCY)
        has_amount = "amount" in columns or ("debit" in columns and "credit" in columns)
        if "booking_date" not in columns or not has_amount:
            return None
        return columns

    def _amount(self, row, columns):
        def cell(role):
            index = columns.get(role)
            return row[index] if index is not None and index < len(row) else ""

        if "amount" in columns:
            return parse_amount(cell("amount"))
        debit, credit = parse_amount(cell("debit")), parse_amount(cell("credit"))
        if debit is None and credit is None:
            return None
        return (credit or Decimal("0.00")) - abs(debit or Decimal("0.00"))

    def parse_statement(self, path: Path) -> ParsedStatement:
        text = self._decode(Path(path).read_bytes())
        rows, header_index, columns = self._locate_header(text)

        def cell(row, role):
            index = columns.get(role)
            return row[index].strip() if index is not None and index < len(row) else ""

        parsed = []
        balances = []
        for row in rows[header_index + 1:]:
            booking_date = parse_date(cell(row, "booking_date"))
            amount = self._amount(row, columns)
            if booking_date is None or amount is None:
                continue  # Leerzeilen, Zwischensummen und Fußzeilen
            if any(word in normalize_comparison_text(cell(row, "status")) for word in PENDING):
                continue  # vorgemerkte Umsätze sind noch nicht gebucht
            payee, payer = cell(row, "payee"), cell(row, "payer")
            counterparty = (payee or payer) if amount < 0 else (payer or payee)
            value_date = parse_date(cell(row, "value_date")) or booking_date
            currency = cell(row, "currency").upper()
            parsed.append(ParsedTransaction(
                booking_date=booking_date.isoformat(),
                value_date=value_date.isoformat(),
                booking_type=cell(row, "booking_type"),
                counterparty=counterparty,
                description=cell(row, "description"),
                amount=str(amount),
                currency=currency if re.fullmatch(r"[A-Z]{3}", currency) else "EUR",
                source_page=1,
            ))
            balances.append(parse_amount(cell(row, "balance")))
        if not parsed:
            raise ValueError("In der CSV-Datei wurden keine Buchungen erkannt.")

        opening = closing = None
        if "balance" in columns and None not in balances:
            ascending = parsed[0].booking_date <= parsed[-1].booking_date
            oldest, newest = (0, -1) if ascending else (-1, 0)
            closing = balances[newest]
            opening = balances[oldest] - Decimal(parsed[oldest].amount)
        return ParsedStatement(transactions=parsed, opening_balance=opening, closing_balance=closing)
