"""Versioned, small development-task suite; acceptance tests are withheld at runtime."""

SUITE_VERSION = "ibex-dev-v1"
CASES = [
    {
        "id": "money-total", "category": "decimal", "target_file": "app/cart_total.py",
        "issue": "Create app/cart_total.py with calc_total(items: list[dict]) -> str. Each item has price (nonnegative finite decimal string) and qty (nonnegative int, bool rejected). Return the sum rounded once to 2 decimals with ROUND_HALF_UP. Empty list returns '0.00'. Raise ValueError for invalid price/qty. Do not mutate inputs. Use Decimal, add and run tests.",
        "tests": """
from app.cart_total import calc_total
def test_basic():
    assert calc_total([{'price':'1.50','qty':3},{'price':'2.00','qty':1}]) == '6.50'
def test_empty_and_round_once():
    assert calc_total([]) == '0.00'
    assert calc_total([{'price':'0.005','qty':2}]) == '0.01'
def test_precision():
    assert calc_total([{'price':'1234567890123.45','qty':2}]) == '2469135780246.90'
def test_invalid():
    for item in [{'price':'-1','qty':1},{'price':'NaN','qty':1},{'price':'Infinity','qty':1},{'price':'bad','qty':1},{'price':'1','qty':-1},{'price':'1','qty':True},{'price':'1','qty':1.5}]:
        with pytest.raises(ValueError): calc_total([item])
def test_no_mutation():
    data=[{'price':'1.20','qty':2}]
    assert calc_total(data) == '2.40'
    assert data == [{'price':'1.20','qty':2}]
""",
    },
    {
        "id": "discount-rounding", "category": "decimal", "target_file": "app/discount_eval.py",
        "issue": "Create app/discount_eval.py with apply_discount(amount: str, rate: str) -> str. rate is fraction removed, between 0 and 1 inclusive. amount must be nonnegative and both inputs finite Decimal strings. Return amount*(1-rate), ROUND_HALF_UP to exactly two decimal places. Invalid inputs raise ValueError. Add and run tests.",
        "tests": """
from app.discount_eval import apply_discount
def test_discount():
    assert apply_discount('19.99','0.15') == '16.99'
def test_rounding():
    assert apply_discount('1.005','0') == '1.01'
def test_bounds():
    assert apply_discount('10','1') == '0.00'
    assert apply_discount('0','0.5') == '0.00'
def test_invalid():
    for a,r in [('-1','0'),('1','-0.1'),('1','1.1'),('NaN','0'),('1','Infinity'),('bad','0')]:
        with pytest.raises(ValueError): apply_discount(a,r)
""",
    },
    {
        "id": "pagination-bugfix", "category": "bugfix", "target_file": "app/page_eval.py",
        "setup": {"app/page_eval.py": "def paginate(items: list, page: int, size: int) -> dict:\n    \"\"\"Return a page, currently with indexing and validation bugs.\"\"\"\n    return {'items': items[page*size:(page+1)*size], 'total': len(items), 'pages': len(items)//size}\n"},
        "issue": "Fix app/page_eval.py paginate(items: list, page: int, size: int) -> dict. Page numbering starts at 1. size is 1..100 inclusive; page >=1. Both must be integers, bool rejected; invalid values raise ValueError. Return items slice, total count, pages as ceiling(total/size), with pages=0 for empty input. Out-of-range pages return empty items; do not mutate input. Add and run regression tests.",
        "tests": """
from app.page_eval import paginate
def test_first_last():
    assert paginate([1,2,3,4,5],1,2) == {'items':[1,2],'total':5,'pages':3}
    assert paginate([1,2,3,4,5],3,2) == {'items':[5],'total':5,'pages':3}
def test_empty_and_beyond():
    assert paginate([],1,2) == {'items':[],'total':0,'pages':0}
    assert paginate([1],2,1) == {'items':[],'total':1,'pages':1}
def test_invalid():
    for p,s in [(0,1),(1,0),(1,101),(True,2),(1,False),(1.5,2),(1,2.5)]:
        with pytest.raises(ValueError): paginate([1],p,s)
def test_no_mutation():
    data=[1,2,3]
    paginate(data,2,1)
    assert data == [1,2,3]
""",
    },
    {
        "id": "idempotent-events", "category": "state", "target_file": "app/event_eval.py",
        "issue": "Create app/event_eval.py with EventLedger. Constructor takes no arguments. apply(event_id: str, amount: str) -> bool adds a nonnegative finite Decimal amount only once per nonempty event_id; returns True for a new event, False for an exact duplicate (Decimal-equal amount), raises ValueError if reused with a different amount or invalid input. Failed inputs must not reserve the ID or change balance. total() -> str returns balance at two decimals ROUND_HALF_UP. Instances are independent. No database/concurrency required. Add and run tests.",
        "tests": """
from app.event_eval import EventLedger
def test_dedup():
    ledger=EventLedger()
    assert ledger.apply('a','1.20') is True
    assert ledger.apply('a','1.2') is False
    assert ledger.total() == '1.20'
def test_conflict():
    ledger=EventLedger(); ledger.apply('a','1')
    with pytest.raises(ValueError): ledger.apply('a','2')
    assert ledger.total() == '1.00'
def test_invalid_not_reserved():
    ledger=EventLedger()
    for amount in ['NaN','Infinity','-1','bad']:
        with pytest.raises(ValueError): ledger.apply('bad-id',amount)
    assert ledger.apply('bad-id','2') is True
    with pytest.raises(ValueError): ledger.apply('','1')
    assert ledger.total() == '2.00'
def test_independent_and_round_once():
    a,b=EventLedger(),EventLedger()
    a.apply('a','0.005'); a.apply('b','0.005')
    assert a.total() == '0.01' and b.total() == '0.00'
""",
    },
    {
        "id": "fastapi-validation", "category": "api", "target_file": "app/quote_eval.py",
        "issue": "Create app/quote_eval.py exporting a FastAPI app. POST /quote accepts JSON {unitPrice: decimal string, quantity: integer}. unitPrice must be finite and >=0; quantity strictly integer 1..100 (reject bool). Return {total: two-decimal string} using Decimal and ROUND_HALF_UP. Invalid or missing fields return HTTP 422. GET /health returns {status: 'ok'}. Add and run TestClient tests; no server process needed.",
        "tests": """
from fastapi.testclient import TestClient
from app.quote_eval import app
def test_quote():
    with TestClient(app) as c:
        r=c.post('/quote',json={'unitPrice':'1.005','quantity':2})
        assert r.status_code == 200 and r.json() == {'total':'2.01'}
def test_health():
    with TestClient(app) as c:
        assert c.get('/health').json() == {'status':'ok'}
def test_invalid():
    with TestClient(app) as c:
        for body in [{},{'unitPrice':'1'},{'unitPrice':'-1','quantity':1},{'unitPrice':'NaN','quantity':1},{'unitPrice':'1','quantity':0},{'unitPrice':'1','quantity':101},{'unitPrice':'1','quantity':True},{'unitPrice':'1','quantity':1.5}]:
            assert c.post('/quote',json=body).status_code == 422
""",
    },
    {
        "id": "retry-boundaries", "category": "resilience", "target_file": "app/retry_eval.py",
        "issue": "Create app/retry_eval.py with retry_call(operation, *, attempts: int=3, base_delay: float=0.1, sleep=time.sleep). Call operation with no args, return its result. Retry ONLY ConnectionError and TimeoutError, at most attempts total calls; wait base_delay*2**retry_index before each retry, no sleep after final failure. Propagate final original exception. attempts must be positive int (not bool); base_delay finite and >=0. Invalid configuration raises ValueError before operation. sleep is injectable for tests. This delay is not money. Add type hints, docstrings and tests.",
        "tests": """
from app.retry_eval import retry_call
def test_eventual_success():
    calls=[]; waits=[]
    def op():
        calls.append(1)
        if len(calls)<3: raise ConnectionError('temporary')
        return 'ok'
    assert retry_call(op,attempts=3,base_delay=.25,sleep=waits.append) == 'ok'
    assert len(calls)==3 and waits==[.25,.5]
def test_no_retry_other_error():
    calls=[]; waits=[]
    def op():
        calls.append(1); raise ValueError('bad')
    with pytest.raises(ValueError): retry_call(op,sleep=waits.append)
    assert calls==[1] and waits==[]
def test_exhaustion():
    waits=[]; error=TimeoutError('final')
    def op(): raise error
    with pytest.raises(TimeoutError) as caught: retry_call(op,attempts=2,base_delay=1,sleep=waits.append)
    assert caught.value is error and waits==[1]
def test_config_before_execution():
    def op(): raise AssertionError('must not execute')
    for attempts,delay in [(0,1),(True,1),(1,-1),(1,float('nan')),(1,float('inf'))]:
        with pytest.raises(ValueError): retry_call(op,attempts=attempts,base_delay=delay)
""",
    },
]
