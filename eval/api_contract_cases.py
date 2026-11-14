"""Two new FastAPI task variants, frozen before model execution.

Visible contracts and external checks use separate concrete samples. Both were
authored by the coding assistant, not reviewed by a human or double-blind panel.
No reference implementation or external test is seeded into candidate sandboxes.
"""
from agent.contracts import MaintainedContract

SUITE_VERSION = "ibex-maintained-api-v1"
PROVENANCE = "Assistant-authored before execution; reference and mutation checked; no human sign-off"


def http_tests(module: str, route: str, valid: list, invalid: list) -> str:
    """Render explicit expected examples without calculating answers from candidate code."""
    return f'''from fastapi.testclient import TestClient
from app.{module} import app
client = TestClient(app, raise_server_exceptions=False)
def test_valid_contract():
    for payload, expected in {valid!r}:
        r = client.post({route!r}, json=payload)
        assert r.status_code == 200, (payload, r.status_code, r.text)
        assert r.json() == expected, (payload, r.json(), expected)
def test_invalid_contract():
    for payload in {invalid!r}:
        r = client.post({route!r}, json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
def test_health_contract():
    r = client.get('/health')
    assert r.status_code == 200 and r.json() == {{'status': 'ok'}}
'''


SHIPPING_REQUEST = """Create app/shipping_api.py exposing FastAPI app. Implement POST /shipping-quote
with JSON object fields weightKg (strict string parsing to a finite Decimal, 0 < weight <= 30),
zone (exact string 'local' or 'remote'), insured (strict JSON boolean, optional default false).
Reject missing required fields, wrong types, invalid values, and extra fields with HTTP 422.
Fee: local base 5.00, remote base 12.00, plus 2.00 per started kilogram ABOVE the first kilogram
(ceil(weightKg - 1), minimum zero), plus 3.00 if insured. Use Decimal for all money.
Return HTTP 200 exactly {'fee': '<two decimal digits>'}. Do not restrict valid weight decimal places.
GET /health returns exactly {'status':'ok'}. Add and run tests; do not use external services."""

BOOKING_REQUEST = """Create app/booking_api.py exposing FastAPI app. Implement POST /booking-check
with JSON object date (strict canonical YYYY-MM-DD string for a real calendar date),
startMinute, endMinute, seats (all strict JSON integers, reject bool/strings/fractions).
Require 0 <= startMinute < endMinute <= 1440; both endpoints are multiples of 15;
15 <= endMinute-startMinute <= 180 inclusive; 1 <= seats <= 8 inclusive.
Reject missing fields, extra fields, wrong types, invalid dates/ranges with HTTP 422.
Return HTTP 200 exactly {'date': original date, 'durationMinutes': end-start,
'seatMinutes': (end-start)*seats}. No persistence, current-date restriction or external services.
GET /health returns exactly {'status':'ok'}. Add and run tests."""

SHIP_VALID = [
    ({"weightKg":"0.25","zone":"local"},{"fee":"5.00"}),
    ({"weightKg":"1.001","zone":"remote","insured":True},{"fee":"17.00"}),
    ({"weightKg":"2","zone":"local"},{"fee":"7.00"}),
    ({"weightKg":"30","zone":"local"},{"fee":"63.00"}),
]
SHIP_INVALID = [{"weightKg":v,"zone":"local"} for v in ["0","-1","30.001","NaN","Infinity","bad",True,1,None]] + [
    {}, {"weightKg":"1"}, {"weightKg":"1","zone":"LOCAL"},
    {"weightKg":"1","zone":"local","insured":1},
    {"weightKg":"1","zone":"local","insured":"false"},
    {"weightKg":"1","zone":"local","extra":1},
]
SHIP_EXTERNAL_VALID = [
    ({"weightKg":"1","zone":"remote"},{"fee":"12.00"}),
    ({"weightKg":"0.0001","zone":"local","insured":True},{"fee":"8.00"}),
    ({"weightKg":"2.0001","zone":"local"},{"fee":"9.00"}),
    ({"weightKg":"29.5","zone":"remote","insured":True},{"fee":"73.00"}),
]
SHIP_EXTERNAL_INVALID = [{"weightKg":v,"zone":"remote"} for v in ["-0.01","30.0000001","sNaN","-Infinity","1x",True,False,2.5,[],{}]] + [
    {"weightKg":"0.5","zone":"remote","insured":None},
    {"weightKg":"0.5","zone":"remote","insured":"true"},
    {"weightKg":"0.5","zone":"remote","memo":"x"},
    {"weightKg":"0.5","zone":None}, {"zone":"remote"},
]

BOOK_VALID = [
    ({"date":"2024-02-29","startMinute":0,"endMinute":15,"seats":1},
     {"date":"2024-02-29","durationMinutes":15,"seatMinutes":15}),
    ({"date":"2026-11-14","startMinute":1260,"endMinute":1440,"seats":8},
     {"date":"2026-11-14","durationMinutes":180,"seatMinutes":1440}),
]
BOOK_BASE = {"date":"2026-01-01","startMinute":60,"endMinute":90,"seats":2}
BOOK_INVALID = [{**BOOK_BASE,key:value} for key,value in [
    ("date","2025-02-29"),("date","20260101"),("date",None),
    ("startMinute",False),("startMinute",-15),("startMinute",61),
    ("endMinute",60),("endMinute",255),("endMinute",1455),
    ("seats",True),("seats","2"),("seats",2.5),("seats",0),("seats",9),
]] + [{}, {**BOOK_BASE,"extra":1}]
BOOK_EXTERNAL_VALID = [
    ({"date":"2000-02-29","startMinute":30,"endMinute":210,"seats":3},
     {"date":"2000-02-29","durationMinutes":180,"seatMinutes":540}),
    ({"date":"2030-12-31","startMinute":1425,"endMinute":1440,"seats":8},
     {"date":"2030-12-31","durationMinutes":15,"seatMinutes":120}),
    ({"date":"1999-01-01","startMinute":600,"endMinute":645,"seats":1},
     {"date":"1999-01-01","durationMinutes":45,"seatMinutes":45}),
]
BOOK_EXTERNAL_BASE = {"date":"2031-03-01","startMinute":300,"endMinute":360,"seats":4}
BOOK_EXTERNAL_INVALID = [{**BOOK_EXTERNAL_BASE,key:value} for key,value in [
    ("date","1900-02-29"),("date","2031-W09-6"),("date",20310301),
    ("startMinute",True),("startMinute","300"),("startMinute",300.5),
    ("endMinute",False),("endMinute",300),("endMinute",301),("endMinute",495),
    ("seats",True),("seats",False),("seats",-2),("seats",10),
]] + [{**BOOK_EXTERNAL_BASE,"comment":"unused"},
       {k:v for k,v in BOOK_EXTERNAL_BASE.items() if k!='seats'}]

CASES = [
    {"id":"shipping-api","category":"fastapi","target_file":"app/shipping_api.py",
     "issue":SHIPPING_REQUEST,"tests":http_tests("shipping_api","/shipping-quote",SHIP_EXTERNAL_VALID,SHIP_EXTERNAL_INVALID)},
    {"id":"booking-api","category":"fastapi","target_file":"app/booking_api.py",
     "issue":BOOKING_REQUEST,"tests":http_tests("booking_api","/booking-check",BOOK_EXTERNAL_VALID,BOOK_EXTERNAL_INVALID)},
]
CONTRACTS = {
    "shipping-api": MaintainedContract("shipping-v1",SHIPPING_REQUEST,
        ("Strict input types, finite Decimal range and no extra fields", "Started kilogram fee and insurance", "Exact HTTP outputs and health"),
        http_tests("shipping_api","/shipping-quote",SHIP_VALID,SHIP_INVALID),PROVENANCE),
    "booking-api": MaintainedContract("booking-v1",BOOKING_REQUEST,
        ("Canonical real date and strict integers", "Quarter-hour endpoints and inclusive duration/seats bounds", "Exact response and health; no extra fields"),
        http_tests("booking_api","/booking-check",BOOK_VALID,BOOK_INVALID),PROVENANCE),
}
