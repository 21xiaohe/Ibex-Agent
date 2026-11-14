"""New task types for checking improvements beyond the six diagnosed tasks.

Frozen before the first post-repair full run; no results are fed to the agent.
Small supplemental validation, not a statistically independent benchmark.
"""
SUITE_VERSION = "ibex-new-tasks-v1"
CASES = [
    {"id":"merge-ranges","category":"algorithm","target_file":"app/ranges_eval.py",
     "issue":"Create app/ranges_eval.py with merge_ranges(intervals: list[tuple[int,int]]) -> list[tuple[int,int]]. Each interval is closed [start,end], with strict integers (bool rejected), start<=end. Sort and merge overlapping or touching endpoints, but do not merge integer-adjacent disjoint intervals: (1,2),(3,4) stay separate. Return tuples; [] -> []; invalid entries raise ValueError. Do not mutate the input. Add and run tests.",
     "tests":"""
from app.ranges_eval import merge_ranges
def test_merge():
    assert merge_ranges([(5,8),(1,3),(3,6),(10,10)]) == [(1,8),(10,10)]
def test_disjoint_empty_nested():
    assert merge_ranges([])==[]
    assert merge_ranges([(1,2),(3,4)])==[(1,2),(3,4)]
    assert merge_ranges([(1,8),(2,3),(1,8)])==[(1,8)]
def test_invalid():
    for data in [[(3,1)],[(True,2)],[(1,2.5)],[(1,)]]:
        with pytest.raises(ValueError): merge_ranges(data)
def test_no_mutation():
    data=[(4,5),(1,4)]; before=data[:]
    merge_ranges(data)
    assert data==before
"""},
    {"id":"inventory-transaction","category":"state","target_file":"app/inventory_eval.py",
     "issue":"Create app/inventory_eval.py class Inventory(initial: dict[str,int]). Copy initial counts, requiring nonnegative strict ints (bool rejected). reserve(request: dict[str,int]) -> None atomically subtracts requested stock; request quantities must be positive strict ints, unknown SKU or insufficient stock raises ValueError. Any failure leaves all stock unchanged. Empty request succeeds without changes. snapshot() -> dict[str,int] returns an independent copy. No concurrency/database needed. Add and run tests.",
     "tests":"""
from app.inventory_eval import Inventory
def test_reserve():
    inv=Inventory({'a':3,'b':2}); inv.reserve({'a':2,'b':1})
    assert inv.snapshot()=={'a':1,'b':1}
def test_atomic_failure():
    inv=Inventory({'a':3,'b':1})
    for request in [{'a':1,'b':2},{'a':1,'missing':1},{'a':1,'b':0}]:
        with pytest.raises(ValueError): inv.reserve(request)
        assert inv.snapshot()=={'a':3,'b':1}
def test_aliases():
    original={'a':2}; inv=Inventory(original); original['a']=99
    data=inv.snapshot(); data['a']=44
    inv.reserve({})
    assert inv.snapshot()=={'a':2}
def test_invalid():
    for value in [True,-1,1.5]:
        with pytest.raises(ValueError): Inventory({'a':value})
    inv=Inventory({'a':3})
    for value in [True,0,-1,1.5]:
        with pytest.raises(ValueError): inv.reserve({'a':value})
"""},
    {"id":"utc-normalization","category":"datetime","target_file":"app/time_eval.py",
     "issue":"Create app/time_eval.py normalize_timestamp(value: str) -> str. Accept ISO 8601 datetimes with explicit timezone offset or trailing Z. Reject naive dates/datetimes, invalid strings, non-strings with ValueError. Convert to UTC and return YYYY-MM-DDTHH:MM:SSZ, dropping fractional seconds without rounding. Must handle timezone day rollover. Use standard library datetime, add and run tests.",
     "tests":"""
from app.time_eval import normalize_timestamp
def test_utc():
    assert normalize_timestamp('2025-01-02T03:04:05Z')=='2025-01-02T03:04:05Z'
    assert normalize_timestamp('2025-01-02T03:04:05.999+00:00')=='2025-01-02T03:04:05Z'
def test_rollover():
    assert normalize_timestamp('2025-01-01T00:30:00+01:00')=='2024-12-31T23:30:00Z'
    assert normalize_timestamp('2025-01-01T23:30:00-02:00')=='2025-01-02T01:30:00Z'
def test_invalid():
    for value in ['2025-01-01','2025-01-01T12:00:00','nonsense','',None,123]:
        with pytest.raises(ValueError): normalize_timestamp(value)
"""},
]
