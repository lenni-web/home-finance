"""Trennt Zahlungspartner und Betreff in Buchungstexten, die als Fließtext vorliegen.

Manche Banken (Sparkassen-PDF) drucken Name und Verwendungszweck ohne Trennzeichen
hintereinander. Es gibt daher kein verlässliches Merkmal im Text selbst. Stattdessen
werden Namen verwendet, die der Benutzer bestätigt hat (``PayeeName``) oder die als
Suchtext einer aktiven Regel vorliegen: Beginnt der Text mit einem solchen Namen, ist
er der Zahlungspartner und der Rest der Betreff.
"""

import re

from .models import CategorizationRule, PayeeName
from .text_normalization import normalize_comparison_text

MIN_KEY_LENGTH = 3
MAX_KEY_WORDS = 8
SEPARATORS = " \t,;:/-–"
TOKEN_RE = re.compile(r"\S+")
# Technische Angaben gehören nicht zum Namen des Zahlungspartners.
TECHNICAL_MARKER_RE = re.compile(
    r"(?:Gläubiger-?ID|Gläubiger-Identifikationsnummer|Mandatsreferenz|Mandat|IBAN|BIC)\s*:",
    re.IGNORECASE,
)


def collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def flow_text(counterparty, description):
    """Der gesamte Buchungstext als eine Zeile, wie er im Auszug fortlaufend steht."""
    return collapse(" ".join([counterparty or "", *(description or "").split("\n")]))


def match_length(text, key_words):
    """Länge des Textanfangs, der den normalisierten Wörtern entspricht (nur an Wortgrenzen)."""
    index = 0
    end = None
    for match in TOKEN_RE.finditer(text):
        if index == len(key_words):
            break
        words = normalize_comparison_text(match.group()).split()
        if not words:  # reines Satzzeichen wie "&" oder "-" innerhalb des Namens
            if index == 0:
                return None
            end = match.end()
            continue
        if words != key_words[index:index + len(words)]:
            return None
        index += len(words)
        end = match.end()
    return end if index == len(key_words) and end else None


def _split_at(text, end):
    return text[:end].rstrip(SEPARATORS), text[end:].lstrip(SEPARATORS)


def candidate_keys():
    """Bekannte Namen als Wortlisten, längste zuerst (der spezifischste Treffer gewinnt)."""
    keys = set(PayeeName.objects.values_list("key", flat=True))
    for text in CategorizationRule.objects.filter(active=True).values_list("match_text", flat=True):
        keys.add(normalize_comparison_text(text))
    words = [
        key.split() for key in keys
        if len(key) >= MIN_KEY_LENGTH and len(key.split()) <= MAX_KEY_WORDS
    ]
    return sorted(words, key=lambda item: (-len(item), -sum(len(word) for word in item)))


def apply_split(counterparty, description, candidates):
    """Liefert (Zahlungspartner, Betreff); ohne bekannten Namen bleibt der Text unverändert."""
    flow = flow_text(counterparty, description)
    for key_words in candidates:
        end = match_length(flow, key_words)
        if end:
            return _split_at(flow, end)
    marker = TECHNICAL_MARKER_RE.search(counterparty or "")
    if marker and marker.start() > 0:
        head = counterparty[:marker.start()].rstrip(SEPARATORS)
        tail = counterparty[marker.start():].strip()
        return head, "\n".join(part for part in (tail, description) if part)
    return counterparty, description


def learn_payee(old_counterparty, old_description, new_counterparty):
    """Merkt den vom Benutzer gekürzten Namen. Gibt (Betreff, Name) zurück oder ``None``.

    Gelernt wird nur, wenn der neue Name ein echter Anfang des ursprünglichen Textes ist.
    """
    key = normalize_comparison_text(new_counterparty)
    words = key.split()
    if len(key) < MIN_KEY_LENGTH or len(words) > MAX_KEY_WORDS:
        return None
    flow = flow_text(old_counterparty, old_description)
    end = match_length(flow, words)
    if not end:
        return None
    _name, remainder = _split_at(flow, end)
    if not remainder:
        return None  # der Name ist der ganze Text: nichts zu trennen
    name = collapse(new_counterparty)[:120]
    PayeeName.objects.get_or_create(key=key, defaults={"name": name})
    return remainder, name


def resplit_pending(statement, exclude_ids=()):
    """Wendet bekannte Namen auf die noch unbestätigten Buchungen eines Auszugs an."""
    candidates = candidate_keys()
    changed = 0
    pending = statement.transactions.filter(reviewed=False).exclude(pk__in=exclude_ids)
    for item in pending:
        counterparty, description = apply_split(item.counterparty, item.description, candidates)
        if (counterparty, description) != (item.counterparty, item.description):
            item.counterparty, item.description = counterparty, description
            item.save(update_fields=["counterparty", "description", "updated_at"])
            changed += 1
    return changed
