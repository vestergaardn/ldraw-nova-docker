import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'web/backend'))
import pytest
from purchasing.quantity_policy import parse_policy, load
from purchasing.lego import LegoCatalogue

CONSTANT='/_next/static/chunks/3287-abcdef.js'
CONSUMER='/_next/static/chunks/6308-123abc.js'
HTML=f'<script src="{CONSTANT}"></script><script src="{CONSUMER}"></script>'
SCRIPTS={CONSTANT:'75748:(e,t,a)=>{a.d(t,{vM:()=>i});let r=Object.freeze({PAB:"11998",BAP:"11996"}),i=999,s=50},79067:()=>{}',
         CONSUMER:'var d=a(75748);let i=t.maxOrderQuantity||d.vM;'}


def test_published_default_has_exact_source_hashes_and_changed_bundle_fails():
    policy=parse_policy(HTML,SCRIPTS,now=1000)
    assert policy['max_quantity']==999 and policy['checked_at']==1000
    assert len(policy['scripts'])==2 and all(len(h)==64 for h in policy['scripts'].values())
    for scripts in [{}, {**SCRIPTS,CONSTANT:SCRIPTS[CONSTANT].replace('999','99')},
                    {**SCRIPTS,CONSUMER:SCRIPTS[CONSUMER].replace('||d.vM','||d.other')},
                    {**SCRIPTS,CONSUMER:SCRIPTS[CONSUMER].replace('a(75748)','a(99999)')}]:
        with pytest.raises(ValueError):parse_policy(HTML,scripts)


def test_null_override_uses_verified_default_but_missing_field_and_failed_policy_do_not():
    policy=parse_policy(HTML,SCRIPTS)
    row=dict(id='300121',designId='3001',name='Brick',availability='AVAILABLE',facets={'color':{'key':'21'}},
             price={'centAmount':21,'currencyCode':'USD'},maxOrderQuantity=None)
    def response(payload):return {'data':{'searchElements':{'total':1,'count':1,'results':[row]}}}
    provider=LegoCatalogue(transport=response,quantity_policy=lambda:policy)
    element=provider.refresh(['300121'])['300121']
    assert element.max_quantity==999 and element.max_quantity_evidence==policy
    row['maxOrderQuantity']=10
    assert provider.refresh(['300121'])['300121'].max_quantity==10
    row['maxOrderQuantity']=0
    assert provider.refresh(['300121'])['300121'].max_quantity==0
    del row['maxOrderQuantity']
    assert provider.refresh(['300121'])['300121'].max_quantity is None
    row['maxOrderQuantity']=None
    def failed():raise ValueError('Changed shop policy')
    assert LegoCatalogue(transport=response,quantity_policy=failed).refresh(['300121'])['300121'].max_quantity is None
