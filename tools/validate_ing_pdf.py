#!/usr/bin/env python3
import argparse
import json
from collections import Counter
from pathlib import Path

from ledger.importers import INGStatementParser


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validiert einen echten ING-Kontoauszug ausschließlich lokal."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, help="Optional: privates Prüfergebnis als JSON")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.pdf.is_file() or args.pdf.suffix.casefold() != ".pdf":
        parser.error("Bitte ein vorhandenes PDF angeben.")
    if args.output and args.output.exists() and not args.force:
        parser.error("Die Ausgabedatei existiert bereits; bei Bedarf --force verwenden.")

    transactions = INGStatementParser().parse_pdf(args.pdf)
    pages = Counter(item.source_page for item in transactions)
    incomplete = [
        index for index, item in enumerate(transactions, start=1)
        if not item.booking_type or not item.counterparty
    ]
    print(f"Erkannte Buchungen: {len(transactions)}")
    print("Pro Seite: " + ", ".join(f"{page}={count}" for page, count in sorted(pages.items())))
    print(f"Unvollständige Buchungen: {len(incomplete)}")
    if incomplete:
        print("Betroffene laufende Nummern: " + ", ".join(map(str, incomplete)))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps([item.to_dict() for item in transactions], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        args.output.chmod(0o600)
        print(f"Privates Prüfergebnis geschrieben: {args.output}")
        print("Diese Datei enthält echte Buchungsdaten und darf nicht weitergegeben werden.")
    return 0 if transactions and not incomplete else 2


if __name__ == "__main__":
    raise SystemExit(main())

