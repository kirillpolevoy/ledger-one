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
