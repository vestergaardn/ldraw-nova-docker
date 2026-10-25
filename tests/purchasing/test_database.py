"""Integration tests require an explicitly isolated PostgreSQL lego_test database."""
import json
import os
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'web/backend'))
import pytest
psycopg=pytest.importorskip('psycopg')
from purchasing.database import Database
from purchasing.domain import CatalogueElement,IdentityIndex,PartRequirement,build_quote

@pytest.fixture(scope='module')
def db():
 url=os.environ.get('LEGO_TEST_DATABASE_URL')
 if not url:pytest.skip('Set LEGO_TEST_DATABASE_URL for isolated integration tests')
 with psycopg.connect(url,autocommit=True) as c:
  assert c.execute('select current_database()').fetchone()[0]=='lego_test'
  for role in ['anon','authenticated','service_role']:
   if not c.execute('select 1 from pg_roles where rolname=%s',(role,)).fetchone():c.execute(f'create role {role}')
  if not c.execute("select 1 from pg_namespace where nspname='lego'").fetchone():
   c.execute((Path(__file__).resolve().parents[2]/'supabase/migrations/20261025001139_purchasing_catalogue.sql').read_text())
  c.execute((Path(__file__).resolve().parents[2]/'supabase/migrations/20261025010718_immutable_revision_quotes.sql').read_text())
  c.execute("alter role lego_app login password 'test-only-password'")
 d=Database(url.replace('postgres:', 'lego_app:').split('@')[0]+'@'+url.split('@')[1])
 # Use a real restricted role; production credentials must never enter this fixture.
 from urllib.parse import urlsplit,urlunsplit
 u=urlsplit(url);d=Database(urlunsplit((u.scheme,'lego_app:test-only-password@'+u.netloc.split('@')[1],u.path,u.query,u.fragment)))
 return d

def seed(db):
 i=IdentityIndex();i.evidence='isolated-fixture';i.parts['3001'].add('3001');i.colors['4'].add('4');i.lego_colors['4'].add('21');i.elements['3001','4'].add('300121');i.element_identity['300121']=('3001','4')
 identity=db.import_identities(i,{})
 now=time.time();elements={str(500000+n):CatalogueElement(str(500000+n),'other','21','Other','available',1,now) for n in range(1000)}
 elements['300121']=CatalogueElement('300121','3001','21','Brick','available',21,now,5)
 run=db.start_run();db.promote(run,elements,identity)
 return run,i,elements

def test_atomic_snapshot_and_failure_preserve_last_complete(db):
 run,i,elements=seed(db)
 failed=db.start_run()
 with pytest.raises(ValueError):db.promote(failed,{'300121':elements['300121']})
 db.fail_run(failed,'fixture interrupted')
 with db.connection() as c:assert str(c.execute("select run_id from lego.current_catalogue where market='US'").fetchone()['run_id'])==run
 index,catalogue=db.inputs([PartRequirement('3001','4',2)])
 assert catalogue['300121'].unit_price==21
 assert index.resolve(PartRequirement('3001','4',2))[0]==['300121']

def test_cart_claim_is_single_use_but_same_request_can_resume(db):
 _,i,elements=seed(db);req=[PartRequirement('3001','4',2)]
 q=build_quote(uuid.uuid4().hex*2,req,i,elements);db.save_quote(q,'fixture.mpd',q['revision'],req)
 intent=db.create_intent(q['id'],q['review_hash'],False)
 with pytest.raises(ValueError):db.create_intent(q['id'],q['review_hash'],False)
 with pytest.raises(ValueError):db.claim_intent(intent['id'],'incorrect','first-client-request')
 first=db.claim_intent(intent['id'],intent['token'],'first-client-request')
 assert db.claim_intent(intent['id'],intent['token'],'first-client-request')==first
 with pytest.raises(ValueError):db.claim_intent(intent['id'],intent['token'],'other-client-request')
 db.intent_result(intent['id'],intent['token'],'first-client-request',{'state':'complete','items':[{'elementId':'300121','requested':2,'added':2,'missing':0,'in_bag':2}]})
 with pytest.raises(ValueError):db.claim_intent(intent['id'],intent['token'],'first-client-request')

def test_public_roles_have_no_access_and_backend_cannot_change_schema(db):
 with db.connection() as c:
  assert not c.execute("select has_schema_privilege('anon','lego','USAGE') as allowed").fetchone()['allowed']
  assert not c.execute("select has_schema_privilege('authenticated','lego','USAGE') as allowed").fetchone()['allowed']
  assert c.execute("select bool_and(rowsecurity) as protected from pg_tables where schemaname='lego'").fetchone()['protected']
  with pytest.raises(psycopg.errors.InsufficientPrivilege):c.execute('create table lego.not_allowed(id int)')


def test_quotes_and_revisions_are_append_only_for_the_app(db):
 for table in ['model_quotes','model_revisions']:
  with db.connection() as c:
   assert c.execute("select has_table_privilege(current_user,%s,'INSERT') as allowed",('lego.'+table,)).fetchone()['allowed']
   assert not c.execute("select has_table_privilege(current_user,%s,'UPDATE') as allowed",('lego.'+table,)).fetchone()['allowed']
   assert not c.execute("select has_table_privilege(current_user,%s,'DELETE') as allowed",('lego.'+table,)).fetchone()['allowed']


def test_connection_enforces_timeouts_after_connecting(db):
 with db.connection() as c:
  assert c.execute('show statement_timeout').fetchone()['statement_timeout']=='30s'
  assert c.execute('show idle_in_transaction_session_timeout').fetchone()['idle_in_transaction_session_timeout']=='30s'
  assert c.execute('show lock_timeout').fetchone()['lock_timeout']=='5s'

def test_observations_keep_newest_values_and_only_record_real_changes(db):
 _,_,elements=seed(db)
 before=elements['300121']
 old=CatalogueElement('300121','3001','21','Brick','available',9,before.checked_at-10,5)
 current=CatalogueElement('300121','3001','21','Brick','out_of_stock',31,before.checked_at+10,5)
 db.observe({'300121':current})
 db.observe({'300121':old})
 _,catalogue=db.inputs([PartRequirement('3001','4',2)])
 assert catalogue['300121'].unit_price==31 and catalogue['300121'].availability=='out_of_stock'
 with db.connection() as c:
  row=c.execute("select new_value from lego.price_changes where element_id='300121' order by id desc limit 1").fetchone()
  assert row['new_value']['unit_price']==31
