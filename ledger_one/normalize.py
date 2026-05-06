import re

_CITIES = (
    r"seattle|brooklyn|nyc|new york|san francisco|los angeles|chicago|"
    r"boston|austin|portland|denver|miami"
)

_PROCESSOR_PREFIX = re.compile(r"^(sq\s*\*|tst\s*\*|paypal\s*\*|sp\s*\*)\s*", re.IGNORECASE)
_ACH_PREFIX = re.compile(r"^orig\s+co\s+name:\s*", re.IGNORECASE)
# Banks deliver the same ACH transaction in two skeleton forms — the verbose
# "CO ENTRY DESCR:<DESCR> SEC:<SEC> ORIG ID:<digits>" carries the same metadata
# as the terse "<DESCR> <SEC> ID: <digits>". Rewrite the verbose form to the
# terse one (capturing the descr text and SEC code) so both shapes converge,
# regardless of whether descr is digits ("6156130376") or text ("AUTOPAY")
# and regardless of which 3-letter NACHA SEC code (PPD, CCD, WEB, TEL, ...).
_ACH_VERBOSE_LABEL = re.compile(
    r"co\s+entry\s+descr:(.+?)\s+sec:([a-z]{3})\s+orig\s+id:", re.IGNORECASE
)
_AMZN_SUFFIX = re.compile(r"\s*\*[a-z0-9]+$", re.IGNORECASE)
_STORE_NUM = re.compile(r"\s*#\d+\b|\s+store\s+\d+\b|\s+t-?\d+\b", re.IGNORECASE)
_DATE = re.compile(r"\s*\b\d{2}/\d{2}\b")
# Masked account/transaction IDs banks emit as XXXXXX1234 — the trailing 4
# real digits don't form a word boundary against the X's, so _LONG_DIGITS
# can't see them. Strip the whole masked block.
_MASKED_DIGITS = re.compile(r"\bx+\d+\b", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\s*\b\d{4,}\b")
_CITY_STATE = re.compile(rf"\s+(?:{_CITIES})\s+[a-z]{{2}}$", re.IGNORECASE)
_TRAILING_CITY = re.compile(rf"\s+(?:{_CITIES})$", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def normalize_merchant(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = _PROCESSOR_PREFIX.sub("", s)
    s = _ACH_PREFIX.sub("", s)
    s = _ACH_VERBOSE_LABEL.sub(r"\1 \2 id:", s)
    s = _AMZN_SUFFIX.sub("", s)
    s = _STORE_NUM.sub("", s)
    s = _DATE.sub("", s)
    s = _MASKED_DIGITS.sub("", s)
    s = _LONG_DIGITS.sub("", s)
    s = _CITY_STATE.sub("", s)
    s = _TRAILING_CITY.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s
