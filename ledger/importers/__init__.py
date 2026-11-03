from .base import ParsedStatement, ParsedTransaction
from .csv_generic import CSVStatementParser
from .ing import INGStatementParser
from .sparkasse import SparkasseStatementParser

PARSERS = {
    parser.name: parser for parser in (INGStatementParser, SparkasseStatementParser, CSVStatementParser)
}
DEFAULT_PARSER = INGStatementParser.name


def get_parser(name):
    """Liefert die Parserklasse zum gespeicherten Namen (Standard: ING-PDF)."""
    try:
        return PARSERS[name or DEFAULT_PARSER]
    except KeyError:
        raise ValueError(f"Unbekanntes Importformat: {name}") from None


def parser_choices():
    return [(name, parser.label) for name, parser in PARSERS.items()]


__all__ = [
    "CSVStatementParser", "DEFAULT_PARSER", "INGStatementParser", "PARSERS", "ParsedStatement",
    "ParsedTransaction", "SparkasseStatementParser", "get_parser", "parser_choices",
]
