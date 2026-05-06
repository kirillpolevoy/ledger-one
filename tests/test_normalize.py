import pytest
from ledger_one.normalize import normalize_merchant

CASES = [
    ("STARBUCKS #1234 SEATTLE WA", "starbucks"),
    ("STARBUCKS STORE 5678", "starbucks"),
    ("WHOLE FOODS MKT 10234", "whole foods mkt"),
    ("AMZN MKTP US*AB12CD34", "amzn mktp us"),
    ("SQ *BLUE BOTTLE COFFEE", "blue bottle coffee"),
    ("TST* THE HALAL GUYS NYC", "the halal guys"),
    ("PAYPAL *NETFLIX", "netflix"),
    ("SP * OURA RING", "oura ring"),
    ("UBER   TRIP 04/12", "uber trip"),
    ("TRADER JOE'S #142", "trader joe's"),
    ("TARGET T-1234 BROOKLYN NY", "target"),
    ("Venmo payment 20240412 1234567890", "venmo payment"),
    ("   Extra   Spaces  ", "extra spaces"),
    ("", ""),
]

@pytest.mark.parametrize("raw,expected", CASES)
def test_normalize(raw, expected):
    assert normalize_merchant(raw) == expected


# Real-world drift seen for a single merchant (TGS OF CHICAGO) where the bank
# changed how SimpleFIN delivered the description three times in a few weeks:
#   1. plain digits + "PPD ID"
#   2. masked X-prefixed digits
#   3. verbose ACH labels: "ORIG CO NAME:" / "CO ENTRY DESCR:" / "SEC:PPD ORIG ID:"
# All three should collapse to the same merchant_pattern so a single override
# survives format drift.
TGS_VARIANTS = [
    "TGS OF CHICAGO - 6156130376 PPD ID: 1470259040",
    "TGS OF CHICAGO - XXXXXX0376 PPD ID: XXXXXX9040",
    "ORIG CO NAME:TGS OF CHICAGO - CO ENTRY DESCR:6156130376 SEC:PPD ORIG ID:1470259040",
]


def test_ach_format_drift_collapses_to_one_pattern():
    patterns = {normalize_merchant(d) for d in TGS_VARIANTS}
    assert len(patterns) == 1, f"expected one canonical pattern, got: {patterns}"


def test_ach_format_drift_matches_existing_override():
    # The override the user had set up before the bank-format drift; new
    # variants must keep matching it so the auto-categorization keeps working.
    expected = "tgs of chicago - ppd id:"
    for d in TGS_VARIANTS:
        assert normalize_merchant(d) == expected, d


def test_strips_orig_co_name_ach_prefix():
    assert normalize_merchant("ORIG CO NAME:ACME CORP") == normalize_merchant("ACME CORP")


def test_strips_xxx_masked_id_digits():
    # Banks sometimes mask all but the last 4 digits as XXXXXX1234. The plain
    # and masked forms should produce the same pattern.
    assert normalize_merchant("ACME CORP XXXXXX1234") == normalize_merchant("ACME CORP 1234567890")


def test_ach_verbose_label_with_text_descr():
    # PEOPLES GAS is a real example from the data: the verbose form has text
    # in the CO ENTRY DESCR field (AUTOPAY) instead of digits, and should still
    # collapse to the terse form "<merchant> <descr> ppd id:".
    verbose = "ORIG CO NAME:PEOPLES GAS CO ENTRY DESCR:AUTOPAY SEC:PPD ORIG ID:4361613900"
    terse = "PEOPLES GAS AUTOPAY PPD ID: 4361613900"
    assert normalize_merchant(verbose) == normalize_merchant(terse)


def test_ach_verbose_label_works_for_non_ppd_sec_codes():
    # NACHA SEC codes other than PPD: CCD (corporate), WEB, TEL, etc. The
    # verbose form should collapse for any 3-letter SEC code.
    for sec in ("CCD", "WEB", "TEL"):
        verbose = f"ORIG CO NAME:VENDOR CO CO ENTRY DESCR:INVOICE SEC:{sec} ORIG ID:1111111111"
        terse = f"VENDOR CO INVOICE {sec} ID: 1111111111"
        assert normalize_merchant(verbose) == normalize_merchant(terse), sec
