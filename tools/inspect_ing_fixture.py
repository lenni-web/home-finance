#!/usr/bin/env python3
import argparse
import json
from collections import Counter
from pathlib import Path

from ledger.importers import INGStatementParser


def main() -> int:
    parser = argparse.ArgumentParser(description="Prüft eine anonymisierte ING-Layoutdatei.")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path, help="Optionaler Pfad für geparste Buchungen")
    args = parser.parse_args()

    transactions = INGStatementParser().parse_fixture(args.fixture)
    by_page = Counter(item.source_page for item in transactions)
    print(f"Erkannte Buchungen: {len(transactions)}")
    print("Pro Seite: " + ", ".join(f"{page}={count}" for page, count in sorted(by_page.items())))

    if args.output:
        args.output.write_text(
            json.dumps([item.to_dict() for item in transactions], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Prüfergebnis geschrieben: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

