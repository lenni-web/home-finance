#!/usr/bin/env python3
"""Zeigt die *Form* der erkannten Buchungstexte eines Sparkassen-Auszugs, ohne Inhalte.

Buchstaben werden durch ``X`` (groß) und ``x`` (klein), Ziffern durch ``9`` ersetzt.
Satzzeichen und Leerzeichen bleiben erhalten. So lässt sich erkennen, wie Name und
Verwendungszweck getrennt sind (Großschreibung, Trennzeichen, Zeilenaufteilung), ohne
dass Namen, Zwecke, IBANs oder Beträge in die Ausgabe gelangen.
"""

import argparse
import json
import re
from pathlib import Path

from ledger.importers import SparkasseStatementParser


def shape(text: str) -> str:
    text = re.sub(r"[A-ZÄÖÜ]", "X", text)
    text = re.sub(r"[a-zäöüß]", "x", text)
    return re.sub(r"\d", "9", text)


def describe(source: Path, limit: int | None) -> dict:
    statement = SparkasseStatementParser().parse_statement(source)
    items = []
    for transaction in statement.transactions[:limit]:
        items.append({
            "richtung": "ausgang" if transaction.amount.startswith("-") else "eingang",
            "buchungsart": shape(transaction.booking_type),
            "zahlungspartner_zeile": shape(transaction.counterparty),
            "weitere_zeilen": [shape(line) for line in transaction.description.split("\n") if line],
        })
    return {
        "format": "home-finance.statement-shapes.v1",
        "privacy": "nur Zeichenform (X/x/9) und Satzzeichen; keine Inhalte",
        "buchungen": len(statement.transactions),
        "eintraege": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Pfad zum Sparkassen-Kontoauszug (PDF)")
    parser.add_argument("output", type=Path, help="Pfad für die JSON-Ausgabe, z. B. sanitized/shapes.json")
    parser.add_argument("--limit", type=int, default=None, help="Nur die ersten N Buchungen ausgeben")
    parser.add_argument("--force", action="store_true", help="Vorhandene Ausgabedatei überschreiben")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        parser.error("Die Ausgabedatei existiert bereits; bei Bedarf --force verwenden.")
    result = describe(args.source, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(result['eintraege'])} von {result['buchungen']} Buchungen geschrieben: {args.output}")
    print("Bitte die Datei vor dem Weitergeben ansehen: Sie darf nur X, x, 9 und Satzzeichen enthalten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
