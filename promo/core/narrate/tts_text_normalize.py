"""Pre-TTS text normalization shared across both TTS backends.

Public API
----------
- :func:`normalize_for_tts` — currency / percent symbols → words
  (``$1,900`` → ``1,900 dollars``, ``10%`` → ``10 percent``). Runs on
  BOTH backends; the input is what Gemini / ElevenLabs actually receive.
- :func:`normalize_digits_to_words` — Gemini-only second pass that spells
  out remaining numeric tokens (``900`` → ``nine hundred``) so MMS_FA's
  letter-only vocab can align them. Called AFTER ``normalize_for_tts``.

Module is pure: no I/O, no module-level mutable state — only frozen
regex compilations and word-form tuples.
"""

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Currency / percent (both backends)
# ---------------------------------------------------------------------------

# Defensive regex pass applied to each segment's text before the TTS call
# (runs on BOTH backends — ElevenLabs + Gemini). Gemini's prompt tells it to
# write numerals in word form, but a regex fallback catches the common cases
# when Gemini still emits "$1,900" / "10%". Applied at TTS-input only —
# captions come from the normalized text via the alignment, so the word form
# surfaces in captions too (acceptable tradeoff for this sprint; full
# display-vs-spoken decoupling is a later concern). The Gemini path applies
# a second pass (``normalize_digits_to_words``) that spells out remaining
# digit tokens so MMS_FA (letter-only vocab) can align them.
_CURRENCY_RE = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def normalize_for_tts(text: str) -> str:
    """Expand currency / percent symbols into words so ElevenLabs reads cleanly.

    - ``$1,900`` → ``1,900 dollars``
    - ``$4.99`` → ``4.99 dollars``
    - ``10%`` → ``10 percent``
    """
    if not text:
        return text
    text = _CURRENCY_RE.sub(r"\1 dollars", text)
    text = _PERCENT_RE.sub(r"\1 percent", text)
    return text


# ---------------------------------------------------------------------------
#  Digit → English words (Gemini path; needed for MMS_FA alignment)
# ---------------------------------------------------------------------------

# MMS_FA's vocab is 26 lowercase letters + apostrophe. Numeric tokens
# like "900" or "$1,900" have no character-level mapping and cause
# ForcedAlignmentError. The fix is deterministic pre-normalization:
# spell digits out BEFORE both the TTS call and the aligner, so what
# Gemini renders and what the aligner expects are byte-for-byte the
# same word stream.
_ONES_WORDS = (
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS_WORDS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)
_NUMBER_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _int_to_words(n: int) -> str:
    """English cardinal for ``n``. Covers 0..10^9 — the realistic promo
    script range (prices, years, measurements, counts). Beyond that,
    falls back to digits — which will raise ForcedAlignmentError
    downstream, surfacing the edge case rather than silently producing
    unaligned audio."""
    if n == 0:
        return "zero"
    if n < 0:
        return "negative " + _int_to_words(-n)
    if n < 20:
        return _ONES_WORDS[n]
    if n < 100:
        return _TENS_WORDS[n // 10] + (" " + _ONES_WORDS[n % 10] if n % 10 else "")
    if n < 1000:
        out = _ONES_WORDS[n // 100] + " hundred"
        return out + (" " + _int_to_words(n % 100) if n % 100 else "")
    if n < 1_000_000:
        out = _int_to_words(n // 1000) + " thousand"
        return out + (" " + _int_to_words(n % 1000) if n % 1000 else "")
    if n < 1_000_000_000:
        out = _int_to_words(n // 1_000_000) + " million"
        return out + (" " + _int_to_words(n % 1_000_000) if n % 1_000_000 else "")
    return str(n)  # pragmatic fallback; will raise ForcedAlignmentError downstream


def normalize_digits_to_words(text: str) -> str:
    """Expand numeric tokens into English words so both Gemini TTS and
    MMS_FA see the same lexical stream. Called AFTER ``normalize_for_tts``
    has already stripped ``$`` / ``%`` (e.g. ``$1,900`` → ``1,900 dollars``),
    so the input never contains currency sigils. Examples:
        "900 suites"           → "nine hundred suites"
        "1,900 dollars"        → "one thousand nine hundred dollars"
        "33 floors"            → "thirty three floors"
        "2.5 metres"           → "two point five metres"
    Non-numeric text is untouched. Commas inside numbers are stripped;
    decimals become "X point Y1 Y2 ...".
    """
    def _replace(match: "re.Match[str]") -> str:
        token = match.group(0)
        stripped = token.replace(",", "")
        try:
            if "." in stripped:
                int_part, dec_part = stripped.split(".", 1)
                whole_words = _int_to_words(int(int_part)) if int_part else "zero"
                digit_words = " ".join(
                    _ONES_WORDS[int(d)] if int(d) > 0 else "zero"
                    for d in dec_part
                )
                return f"{whole_words} point {digit_words}"
            return _int_to_words(int(stripped))
        except ValueError:
            # Malformed number — leave as-is; MMS_FA will warn/low-score.
            return token
        except OverflowError:
            # Number exceeds Python int range — extremely rare in promo
            # scripts. Warn so operator sees it; return original digits.
            logger.warning(
                "Number %r too large to spell out; leaving as digits. "
                "MMS_FA alignment will score this token as zero-width.",
                token,
            )
            return token

    return _NUMBER_TOKEN_RE.sub(_replace, text)
