"""Check that frozen acceptance tests accept reference behavior and reject broken code.

Reference implementations are test fixtures, never seeded into agent sandboxes.
"""
from pathlib import Path
import shlex
import subprocess
import sys
import pytest
from eval.objective_cases import CASES
from eval.run_objective import acceptance_command

REFERENCES = {
    'money-total': '''
from decimal import Decimal, ROUND_HALF_UP
def calc_total(items: list[dict]) -> str:
    """Sum exact amounts before rounding."""
    total=Decimal(0)
    for item in items:
        try: price=Decimal(item['price'])
        except Exception: raise ValueError('price')
        qty=item['qty']
        if not price.is_finite() or price<0 or type(qty) is not int or qty<0:
            raise ValueError('item')
        total+=price*qty
    return str(total.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
''',
    'discount-rounding': '''
from decimal import Decimal, ROUND_HALF_UP
def apply_discount(amount: str, rate: str) -> str:
    """Apply an exact decimal discount."""
    try: a,r=Decimal(amount),Decimal(rate)
    except Exception: raise ValueError('input')
    if not a.is_finite() or not r.is_finite() or a<0 or not 0<=r<=1:
        raise ValueError('range')
    return str((a*(1-r)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
''',
    'pagination-bugfix': '''
def paginate(items: list, page: int, size: int) -> dict:
    """Return a validated one-based page."""
    if type(page) is not int or type(size) is not int or page<1 or not 1<=size<=100:
        raise ValueError('page')
    return {'items':items[(page-1)*size:page*size],'total':len(items),'pages':(len(items)+size-1)//size}
''',
    'idempotent-events': '''
from decimal import Decimal, ROUND_HALF_UP
class EventLedger:
    def __init__(self) -> None:
        self.events={}; self.balance=Decimal(0)
    def apply(self,event_id: str,amount: str) -> bool:
        """Validate before deduplicating and committing."""
        try: value=Decimal(amount)
        except Exception: raise ValueError('amount')
        if not event_id or not value.is_finite() or value<0: raise ValueError('input')
        if event_id in self.events:
            if self.events[event_id]!=value: raise ValueError('conflict')
            return False
        self.events[event_id]=value; self.balance+=value
        return True
    def total(self) -> str:
        """Round the accumulated balance once."""
        return str(self.balance.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
''',
    'fastapi-validation': '''
from decimal import Decimal, ROUND_HALF_UP
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator
app=FastAPI()
class Quote(BaseModel):
    unitPrice: str
    quantity: int=Field(strict=True,ge=1,le=100)
    @field_validator('unitPrice')
    @classmethod
    def price(cls,value: str) -> str:
        """Reject nonfinite and negative prices."""
        try: amount=Decimal(value)
        except Exception: raise ValueError('price')
        if not amount.is_finite() or amount<0: raise ValueError('price')
        return value
@app.post('/quote')
def quote(body: Quote) -> dict:
    """Return exact price times quantity."""
    return {'total':str((Decimal(body.unitPrice)*body.quantity).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))}
@app.get('/health')
def health() -> dict:
    """Return readiness text."""
    return {'status':'ok'}
''',
    'retry-boundaries': '''
import time,math
from typing import Callable,Any
def retry_call(operation: Callable[[],Any], *, attempts: int=3, base_delay: float=.1, sleep: Callable=time.sleep) -> Any:
    """Retry only transient failures without sleeping after exhaustion."""
    if type(attempts) is not int or attempts<1 or not math.isfinite(base_delay) or base_delay<0:
        raise ValueError('configuration')
    for index in range(attempts):
        try: return operation()
        except (ConnectionError,TimeoutError):
            if index==attempts-1: raise
            sleep(base_delay*2**index)
''',
}


@pytest.mark.parametrize('case', CASES, ids=[c['id'] for c in CASES])
def test_acceptance_oracle_accepts_reference_rejects_broken(case: dict, tmp_path: Path) -> None:
    """A suite syntax/import error or vacuous always-pass scorer fails ordinary CI."""
    app=tmp_path/'app'
    app.mkdir()
    (app/'__init__.py').write_text('',encoding='utf-8')
    target=tmp_path/case['target_file']
    target.write_text(REFERENCES[case['id']],encoding='utf-8')
    command=[sys.executable,*shlex.split(acceptance_command(case,str(tmp_path)))[1:]]
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'IBEX_ACCEPTANCE=' in result.stdout
    target.write_text("raise RuntimeError('broken implementation')\n",encoding='utf-8')
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert result.returncode != 0
