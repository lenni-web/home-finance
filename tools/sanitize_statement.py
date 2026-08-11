#!/usr/bin/env python3
"""Create a privacy-minimised layout fixture from a local bank statement PDF.

The source PDF is read locally. The generated JSON contains no PDF metadata,
images, attachments or original free text. Only an allowlist of structural
terms plus normalised dates and synthetic amounts is retained.
"""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader


SAFE_WORDS = {
    "alter", "auszugsnummer", "betrag", "bic", "buchung", "datum", "dauerauftrag",
    "eingeräumte", "eur", "euro", "girokonto", "gutschrift", "iban", "kontoauszug",
    "kontoüberziehung", "lastschrift", "mandat", "neuer", "nummer", "referenz", "saldo",
    "seite", "valuta", "verwendungszweck", "von",
}
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
AMOUNT_RE = re.compile(r"^(-?)(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})$")
TOKEN_RE = re.compile(
    r"\s+|-?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2}|\d{2}\.\d{2}\.\d{4}"
    r"|[\wÄÖÜäöüß]+(?:\.[\wÄÖÜäöüß]+)*|[^\w\s]",
    re.UNICODE,
)


@dataclass
class Fragment:
    page: int
    x: float
    y: float
    font_size: float
    text: str


class Sanitizer:
    def __init__(self):
        self.private_tokens: dict[str, str] = {}
        self.next_private_token = 1

    def private_placeholder(self, token: str) -> str:
        key = token.casefold()
        if key not in self.private_tokens:
            self.private_tokens[key] = f"[TEXT_{self.next_private_token:03d}]"
            self.next_private_token += 1
        return self.private_tokens[key]

    @staticmethod
    def synthetic_amount(match: re.Match[str]) -> str:
        sign = match.group(1)
        return f"{sign}10,00"

    def token(self, value: str) -> str:
        if value.isspace():
            return value
        if DATE_RE.fullmatch(value):
            return "01.01.2000"
        amount = AMOUNT_RE.fullmatch(value)
        if amount:
            return self.synthetic_amount(amount)
        if value.casefold() in SAFE_WORDS:
            return value
        if value.isdigit():
            return "[NUMMER]"
        if re.fullmatch(r"[^\w\s]", value, re.UNICODE):
            return value
        return self.private_placeholder(value)

    def text(self, value: str) -> str:
        return "".join(self.token(token) for token in TOKEN_RE.findall(value))


def extract_fixture(source: Path) -> dict:
    reader = PdfReader(source)
    if reader.is_encrypted:
        raise ValueError("Das PDF ist verschlüsselt. Bitte zunächst lokal entsperren.")

    sanitizer = Sanitizer()
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        fragments: list[Fragment] = []

        def visit_text(text, _cm, tm, _font_dictionary, font_size):
            sanitized = sanitizer.text(text)
            if sanitized.strip():
                fragments.append(Fragment(
                    page=page_number,
                    x=round(float(tm[4]), 2),
                    y=round(float(tm[5]), 2),
                    font_size=round(float(font_size), 2),
                    text=sanitized,
                ))

        page.extract_text(visitor_text=visit_text)
        pages.append({
            "page": page_number,
            "width": round(float(page.mediabox.width), 2),
            "height": round(float(page.mediabox.height), 2),
            "fragments": [asdict(fragment) for fragment in fragments],
        })

    return {
        "format": "home-finance.statement-layout.v1",
        "privacy": "allowlist-only; free text replaced; dates and amounts synthetic",
        "page_count": len(pages),
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Erzeugt lokal eine anonymisierte Layout-Testdatei aus einem Kontoauszug."
    )
    parser.add_argument("source", type=Path, help="Pfad zum Original-PDF")
    parser.add_argument("output", type=Path, help="Pfad für die anonymisierte JSON-Datei")
    parser.add_argument("--force", action="store_true", help="Vorhandene Ausgabedatei überschreiben")
    args = parser.parse_args()

    if args.source.suffix.casefold() != ".pdf" or not args.source.is_file():
        parser.error("Die Quelldatei muss ein vorhandenes PDF sein.")
    if args.output.exists() and not args.force:
        parser.error("Die Ausgabedatei existiert bereits; bei Bedarf --force verwenden.")
    if args.source.resolve() == args.output.resolve():
        parser.error("Quell- und Ausgabedatei dürfen nicht identisch sein.")

    fixture = extract_fixture(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Anonymisierte Testdatei geschrieben: {args.output}")
    print(f"Enthaltene Seiten: {fixture['page_count']}")
    print("Bitte die JSON-Datei vor dem Weitergeben trotzdem manuell kontrollieren.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
