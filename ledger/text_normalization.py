import re
import unicodedata
from difflib import SequenceMatcher


PDF_REPLACEMENT_CHARACTERS = "\ufffd\u25a1\u25a0"
UMLAUT_VARIANTS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})


def normalize_comparison_text(value):
    """Normalize text for comparison without changing the stored original value."""
    text = (value or "").casefold()
    text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    text = text.translate(UMLAUT_VARIANTS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.translate({ord(character): None for character in PDF_REPLACEMENT_CHARACTERS})
    return " ".join(re.findall(r"[a-z0-9]+", text))


def comparison_words(value, minimum_length=3):
    return {
        word for word in normalize_comparison_text(value).split()
        if len(word) >= minimum_length
    }


def words_match(left, right):
    """Allow one typical OCR/PDF character error only for sufficiently long words."""
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.84


def has_tolerant_word_overlap(left, right):
    return any(words_match(a, b) for a in comparison_words(left) for b in comparison_words(right))


def tolerant_phrase_in_text(needle, haystack):
    normalized_needle = normalize_comparison_text(needle)
    normalized_haystack = normalize_comparison_text(haystack)
    if normalized_needle and normalized_needle in normalized_haystack:
        return True
    needle_words = list(comparison_words(needle, minimum_length=2))
    haystack_words = comparison_words(haystack, minimum_length=2)
    return bool(needle_words) and all(
        any(words_match(needle_word, haystack_word) for haystack_word in haystack_words)
        for needle_word in needle_words
    )
