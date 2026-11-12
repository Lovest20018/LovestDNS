from pathlib import Path
import runpy
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SCRIPTS))
module = runpy.run_path(str(SCRIPTS/'test-dnscrypt-source-function.py'))


def test_workload_uses_both_types_for_each_name():
    rows = [module['question'](index) for index in range(96)]
    assert len(set(rows)) == 96 and len({name for name,_ in rows}) == 48
    for name in {name for name,_ in rows}:
        assert {kind for other,kind in rows if other == name} == {1,28}


@pytest.mark.parametrize('change',['type','name','ttl','address','rcode'])
def test_binary_function_checks_reject_wrong_answers(monkeypatch,change):
    response = {'id':123,'rcode':0,'truncated':False,'answers':[
        {'name':'pool-0.cache.test','class':1,'type':1,'ttl':600,'value':'192.0.2.10'}]}
    if change == 'rcode': response['rcode'] = 3
    else:
        field,value = {'type':('type',28),'name':('name','wrong.cache.test'),
                       'ttl':('ttl',0),'address':('value','192.0.2.11')}[change]
        response['answers'][0][field] = value
    monkeypatch.setitem(module['checked'].__globals__,'query',lambda *a,**kw:response)
    with pytest.raises(ValueError): module['checked'](12345,'pool-0.cache.test',1)
