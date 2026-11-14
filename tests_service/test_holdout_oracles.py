"""Validate supplemental task assertions independently of generated implementations."""
from pathlib import Path
import shlex
import subprocess
import sys
import pytest
from eval.holdout_cases import CASES
from eval.run_objective import acceptance_command

REFERENCES = {
    'merge-ranges': '''
def merge_ranges(intervals: list[tuple[int,int]]) -> list[tuple[int,int]]:
    """Merge overlapping closed intervals without modifying input."""
    for pair in intervals:
        if len(pair)!=2 or any(type(x) is not int for x in pair) or pair[0]>pair[1]:
            raise ValueError('interval')
    out=[]
    for start,end in sorted(intervals):
        if out and start<=out[-1][1]: out[-1]=(out[-1][0],max(end,out[-1][1]))
        else: out.append((start,end))
    return out
''',
    'inventory-transaction': '''
class Inventory:
    def __init__(self,initial: dict[str,int]) -> None:
        if any(type(v) is not int or v<0 for v in initial.values()): raise ValueError('initial')
        self.stock=dict(initial)
    def reserve(self,request: dict[str,int]) -> None:
        """Validate all items before mutating any item."""
        for key,value in request.items():
            if type(value) is not int or value<=0 or key not in self.stock or value>self.stock[key]: raise ValueError('request')
        for key,value in request.items(): self.stock[key]-=value
    def snapshot(self) -> dict[str,int]:
        """Return a defensive copy."""
        return dict(self.stock)
''',
    'utc-normalization': '''
from datetime import datetime,timezone
def normalize_timestamp(value: str) -> str:
    """Normalize aware ISO values to whole UTC seconds."""
    if not isinstance(value,str): raise ValueError('type')
    try: parsed=datetime.fromisoformat(value)
    except (ValueError,TypeError): raise ValueError('format')
    if parsed.tzinfo is None: raise ValueError('naive')
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
''',
}


@pytest.mark.parametrize('case', CASES, ids=[c['id'] for c in CASES])
def test_new_oracles_accept_reference_and_reject_broken(case: dict, tmp_path: Path) -> None:
    """Task tests must pass a valid reference and reject an import-time failure."""
    (tmp_path/'app').mkdir()
    (tmp_path/'app'/'__init__.py').write_text('')
    target=tmp_path/case['target_file']
    target.write_text(REFERENCES[case['id']],encoding='utf-8')
    command=[sys.executable,*shlex.split(acceptance_command(case,str(tmp_path)))[1:]]
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert result.returncode==0, result.stdout+result.stderr
    target.write_text("raise RuntimeError('broken')\n",encoding='utf-8')
    assert subprocess.run(command,capture_output=True,text=True,timeout=15).returncode!=0
