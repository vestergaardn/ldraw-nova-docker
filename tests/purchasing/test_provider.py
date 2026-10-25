from pathlib import Path
import sys
import urllib.error
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'web/backend'))
import pytest
from purchasing.lego import LegoCatalogue,CatalogueError


def row(i):return {'id':str(i),'designId':'3001','facets':{'color':{'key':'21'}},'name':'Brick','availability':'AVAILABLE','price':{'centAmount':21,'currencyCode':'USD'}}
def response(ids,total=1000):return {'data':{'searchElements':{'total':total,'count':len(ids),'results':[row(i) for i in ids]}}}

def test_complete_refuses_page_gaps_duplicates_and_changing_counts():
 for fault in ['gap','duplicate','changed']:
  def transport(payload):
   page=payload['variables']['input']['page'];ids=list(range((page-1)*50+1,page*50+1))
   if page==2:
    if fault=='gap':ids.pop()
    elif fault=='duplicate':ids[0]=1
   return response(ids,1001 if fault=='changed' and page==2 else 1000)
  with pytest.raises(CatalogueError):LegoCatalogue(transport=transport).complete()

def test_403_is_not_retried_and_429_has_bounded_backoff():
 for code,expected in [(403,1),(429,4)]:
  calls=[];sleeps=[]
  def fail(payload):calls.append(payload);raise urllib.error.HTTPError('https://www.lego.com/api/graphql',code,'test',{'Retry-After':'2'},None)
  with pytest.raises(CatalogueError):LegoCatalogue(transport=fail,sleep=sleeps.append).page()
  assert len(calls)==expected and len(sleeps)==expected-1
  assert all(2<=n<=121 for n in sleeps)

def test_missing_batch_ids_are_unlisted_and_different_currency_rejected():
 p=LegoCatalogue(transport=lambda payload:response([1],1))
 assert p.refresh(['1','2'])['2'].availability=='unlisted'
 assert p.refresh(['1','2'])['2'].unit_price is None
 def wrong(payload):
  r=response([1]);r['data']['searchElements']['results'][0]['price']['currencyCode']='EUR';return r
 with pytest.raises(CatalogueError):LegoCatalogue(transport=wrong).page()
