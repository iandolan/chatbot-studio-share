"""Outgoing-copy normalisation.

House style forbids em-dashes and double hyphens in anything a visitor reads.
Two things kept putting them back:

1. The knowledge base is scraped from sites whose copy is full of them, and the
   model mirrors the punctuation of whatever it retrieved.
2. The model reaches for them on its own even when the knowledge is clean.

A system-prompt rule fixes most of (1) and some of (2), but not reliably, so
replies get normalised on the way out. This runs on every reply, including each
streamed chunk, so it has to be cheap and idempotent.
"""
import re

_DASHES = "[–—]"

# "9 AM - 6 PM" needs its own rule: a letter sits before the dash, so the
# digit-to-digit pattern below never matches it.
_CLOCK_RANGE = re.compile(r"(\d(?::\d\d)?\s*[AaPp]\.?[Mm]\.?)\s*" + _DASHES + r"\s*(\d)")
_NUM_RANGE = re.compile(r"(\d)\s*" + _DASHES + r"\s*(\d)")
_DAYS = ("(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)")
_DAY_RANGE = re.compile(r"\b" + _DAYS + r"\s*" + _DASHES + r"\s*" + _DAYS + r"\b")
_EM = re.compile(r"\s*—\s*")
_EN = re.compile(r"\s*–\s*")
_DOUBLE_HYPHEN = re.compile(r"(?<=\w)\s--\s(?=\w)")
_REPEAT_COMMA = re.compile(r",\s*,+")
_COMMA_BEFORE_PUNCT = re.compile(r",\s*([.!?;:])")
_LEADING_COMMA = re.compile(r"(^|\n)[ \t]*,[ \t]*")
# A dash split across stream chunks can leave "word , next"; close that gap.
_SPACE_BEFORE_COMMA = re.compile(r"[ \t]+,")
_RUN_OF_SPACES = re.compile(r"[ \t]{2,}")

_NOTHING_TO_DO = ("—", "–", "--")



def _normalise(text, at_start=True, ranges=True):
    """``at_start`` is False for a mid-stream chunk, where a leading comma is
    the tail of the previous chunk's dash and must survive.

    ``ranges`` is False while streaming. Turning "9 AM - 6 PM" into "9 AM to
    6 PM" needs context on both sides of the dash, which a chunk boundary can
    land anywhere inside; trying to buffer for it was unreliable at some chunk
    sizes and silently mangled the text. The dash-to-comma rules below are
    position-local and always safe, so the stream gets those, and the stored
    reply is normalised in full from the raw text once the stream ends.
    """
    if not text or not any(d in text for d in _NOTHING_TO_DO):
        return text
    if ranges:
        text = _CLOCK_RANGE.sub(r"\1 to \2", text)
        text = _NUM_RANGE.sub(r"\1 to \2", text)
        text = _DAY_RANGE.sub(r"\1 to \2", text)
    text = _EM.sub(", ", text)
    text = _EN.sub(", ", text)
    text = _DOUBLE_HYPHEN.sub(", ", text)
    text = _REPEAT_COMMA.sub(", ", text)
    text = _COMMA_BEFORE_PUNCT.sub(r"\1", text)
    if at_start:
        text = _LEADING_COMMA.sub(r"\1", text)
    else:
        # Still tidy later lines, just not the join to the previous chunk.
        text = _LEADING_COMMA.sub(lambda m: m.group(1) if m.group(1) else m.group(0), text)
    text = _SPACE_BEFORE_COMMA.sub(",", text)
    text = _RUN_OF_SPACES.sub(" ", text)
    return text


def strip_dashes(text):
    """Replace em/en dashes with house-style punctuation.

    Ranges become "to"; everything else becomes a comma, which is how the rule
    reads in practice. Returns the input unchanged when there is nothing to do,
    so the common case costs one scan.
    """
    return _normalise(text, at_start=True)


class DashStripper:
    """Applies strip_dashes across a stream without splitting a dash from its
    surrounding whitespace.

    A dash and the spaces around it can land in different chunks: "the firm "
    then "— and so on". Normalising each chunk alone emits "the firm" plus
    ", and so on", which the visitor reads as "the firm , and so on". So any
    trailing whitespace or dash is held back and prepended to the next chunk.
    """

    __slots__ = ("_carry", "_started")

    def __init__(self):
        self._carry = ""
        self._started = False

    def feed(self, chunk):
        buf = self._carry + (chunk or "")
        if not buf:
            return ""
        # Hold back the trailing run of characters whose normalisation depends
        # on what comes next.
        cut = len(buf)
        while cut > 0 and (buf[cut - 1].isspace() or buf[cut - 1] in "–—-"):
            cut -= 1
        self._carry = buf[cut:]
        out = _normalise(buf[:cut], at_start=not self._started, ranges=False)
        if out:
            self._started = True
        return out

    def flush(self):
        out = _normalise(self._carry, at_start=not self._started, ranges=False)
        self._carry = ""
        if out:
            self._started = True
        return out
