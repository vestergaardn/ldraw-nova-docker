"""Mandatory publication checks, with real geometry inventory and isolated shop data."""
import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import main
import publications
import sandbox
import settings
import tools
from store import ChatStore
from purchasing import service, discovery
from purchasing.domain import CatalogueElement, IdentityIndex, PartRequirement, build_quote, publication_problems

SOURCE = b'0 FILE model.ldr\n0 Stock fixture\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n'


class Shop:
    def __init__(self):
        self.index = IdentityIndex(evidence='verified fixture import')
        self.index.parts['3001'].add('3001')
        for ld, rb, lego, element in [('4','4','21','300121'), ('14','14','24','300124')]:
            self.index.colors[ld].add(rb)
            self.index.lego_colors[rb].add(lego)
            self.index.elements['3001',rb].add(element)
        self.elements = {e: CatalogueElement(e,'3001',c,'Brick','available',21,time.time(),100)
                         for e,c in [('300121','21'),('300124','24')]}
        self.quotes, self.records = {}, {}
        self.installed = False
        self.refreshes = 0
        self.fault = None

    def inputs(self, _requirements): return self.index, dict(self.elements)
    def fees(self): return []
    def observe(self, elements): self.elements.update(elements)
    def save_quote(self, quote, *args): self.quotes[quote['id']] = quote
    def refresh(self, ids):
        self.refreshes += 1
        if self.fault == 'outage': raise RuntimeError('private connection string must not leak')
        if self.fault == 'incomplete': return {}
        return {e: replace(self.elements[e], checked_at=time.time(),
                          availability='out_of_stock' if self.fault == 'stock' else self.elements[e].availability)
                for e in ids}
    def save_publication(self, record, quote, after):
        assert self.quotes[quote['id']] == quote
        assert not publication_problems(quote, after=after)
        if self.fault == 'database': raise RuntimeError('private database URL')
        self.records[record['file']] = record
    def bootstrap_publications(self, records):
        if not self.installed:
            self.records.update({r['file']: r for r in records})
            self.installed = True
        return list(self.records.values())
    def available_candidates(self):
        from dataclasses import asdict
        return [dict(part='3001', color=c, payload=asdict(self.elements[e]), evidence=self.index.evidence)
                for c,e in [('4','300121'),('14','300124')]]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # The agent account needs traversable ancestors in real sandbox tests.
    for path in [tmp_path, *tmp_path.parents]:
        if path == Path('/tmp'): break
        path.chmod(0o755)
    for name, path in [('DATA_DIR',tmp_path/'data'), ('GENERATED_DIR',tmp_path/'data/generated'),
                       ('OUTPUT_DIR',tmp_path/'data/output'), ('CHATS_DIR',tmp_path/'data/chats'),
                       ('CONFIG_DIR',tmp_path/'config')]:
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings,name,path)
    settings.CONFIG_DIR.chmod(0o700)
    db = Shop()
    monkeypatch.setattr(service, 'Database', lambda: db)
    monkeypatch.setattr(service, 'LegoCatalogue', lambda: db)
    monkeypatch.setattr(publications, 'Database', lambda: db)
    monkeypatch.setattr(discovery, 'Database', lambda: db)
    monkeypatch.setattr(discovery, '_cache', None)
    monkeypatch.setattr(service, '_capacity', None)
    monkeypatch.setattr(service, '_interactive_capacity', None)
    monkeypatch.setattr(service, '_model_locks', {})
    monkeypatch.setattr(service, '_loop', None)
    monkeypatch.setattr(publications, '_commit_lock', None)
    publications.initialize(db)
    store = ChatStore(settings.CHATS_DIR, settings.OUTPUT_DIR)
    monkeypatch.setattr(main, 'get_store', lambda: store)
    chat = store.create_chat()
    events = []
    ctx = tools.ToolContext(chat['id'], store, lambda event,data: events.append((event,data)))
    source = ctx.work_dir/'draft.mpd'
    source.write_bytes(SOURCE)
    return ctx, db, source, events


@pytest.fixture
def render_stub(monkeypatch):
    async def review(ctx, args, timeout):
        if args[0] == 'validate': Path(args[-1]).write_text('{}')
        else:
            folder = Path(args[args.index('--outdir')+1])
            (folder/'home.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            (folder/'leocad-bom.csv').write_text('Part ID,Color Code,Quantity\n3001,4,1\n')
        return sandbox.RunResult(0,'passed','',False,0)
    monkeypatch.setattr(tools, '_trusted_review', review)
    return review


def publish(ctx): return asyncio.run(tools.t_publish_model(ctx, 'draft.mpd', 'Stock fixture'))


@pytest.mark.parametrize('fault', ['stock','incomplete','outage','database'])
def test_post_render_failures_publish_nothing_and_keep_draft(setup,render_stub,fault):
    ctx,db,source,events = setup
    db.fault = fault
    result = publish(ctx)
    assert not result.models and json.loads(result.content)['published'] is False
    assert source.read_bytes() == SOURCE and not list(settings.GENERATED_DIR.iterdir())
    assert not any(event == 'model' for event,_ in events)
    assert 'private connection' not in result.content and 'private database' not in result.content
    assert (ctx.work_dir/'stock-check.json').is_file()


def test_snapshot_stays_identical_during_edits_and_cache_resumes(setup,render_stub,monkeypatch):
    ctx,db,source,_ = setup
    calls=[]
    async def review(*args):
        calls.append(args[1][0]); source.write_bytes(SOURCE.replace(b'1 4 ', b'1 14 '))
        return await render_stub(*args)
    monkeypatch.setattr(tools, '_trusted_review', review)
    db.fault='stock'
    assert not publish(ctx).models
    source.write_bytes(SOURCE)
    db.fault=None
    db.elements['300121']=replace(db.elements['300121'],availability='available')
    result=publish(ctx); info=json.loads(result.content)
    assert calls == ['validate','render']  # retry uses only the completed protected render
    model=ctx.store.resolve(ctx.chat_id,result.models[0]['model'])
    assert model.read_bytes()==SOURCE and info['sha256']==hashlib.sha256(SOURCE).hexdigest()
    assert db.refreshes>=2 and publications.approved(model)
    client=TestClient(main.app)
    assert client.get(info['download_url']).content==SOURCE
    assert client.get(info['validation']).status_code==200
    assert client.get(f'/api/chats/{ctx.chat_id}').json()['models']
    assert list(db.records.values())[0]['enforced'] is True


def test_repair_changes_color_then_passes(setup,render_stub):
    ctx,db,source,_=setup
    db.elements['300121']=replace(db.elements['300121'],availability='out_of_stock')
    result=asyncio.run(tools.t_check_build_parts(ctx,'draft.mpd'))
    feedback=json.loads(result.content)
    assert feedback['status']=='blocked'
    assert feedback['available_alternatives'][0]['available_colors'][0]['color']=='14'
    assert not publish(ctx).models
    source.write_bytes(SOURCE.replace(b'1 4 ',b'1 14 '))
    published=publish(ctx)
    assert published.models and json.loads(published.content)['coverage']==1.0


def test_direct_files_archives_chat_downloads_and_hash_changes_cannot_bypass(setup,render_stub):
    ctx,db,source,_=setup
    direct=settings.GENERATED_DIR/'bypass.mpd';direct.write_bytes(SOURCE)
    direct.with_suffix('.png').write_bytes(b'\x89PNG')
    ctx.store.add_model(ctx.chat_id,'bypass',direct,[])
    client=TestClient(main.app)
    assert client.get('/api/models').json()['models']==[]
    assert client.get(f'/api/chats/{ctx.chat_id}').json()['models']=={}
    assert client.get('/files/generated/bypass.mpd').status_code==404
    assert client.get('/api/glb',params={'url':'/files/generated/bypass.mpd'}).status_code==404
    assert client.post('/api/quotes',json={'url':'/files/generated/bypass.mpd'}).status_code==404
    with zipfile.ZipFile(io.BytesIO(client.get('/api/models/zip').content)) as z: assert not z.namelist()
    base=f'/api/chats/{ctx.chat_id}/artifacts/'
    for name in ['draft.mpd','renamed.txt']:
        (ctx.work_dir/name).write_bytes(SOURCE)
        assert client.get(base+name).status_code==409
    with zipfile.ZipFile(ctx.work_dir/'draft.zip','w') as z:z.writestr('model.mpd',SOURCE)
    assert client.get(base+'draft.zip').status_code==409
    result=publish(ctx);model=ctx.store.resolve(ctx.chat_id,result.models[0]['model'])
    assert client.get(base+'draft.mpd').content==SOURCE
    model.write_bytes(SOURCE+b'0 Changed\n')
    assert not publications.approved(model)
    assert client.get('/files/generated/'+model.name).status_code==404


def test_legacy_cutover_is_one_time_and_staleness_does_not_delete_models(setup,render_stub,monkeypatch):
    ctx,db,_,_=setup
    legacy=settings.GENERATED_DIR/'old.mpd';legacy.write_bytes(SOURCE)
    db.installed=False;publications.initialize(db)
    assert publications.approved(legacy) and not publications.record_for(legacy)['enforced']
    later=settings.GENERATED_DIR/'unregistered.mpd';later.write_bytes(SOURCE)
    publications.initialize(db)
    assert not publications.approved(later) and legacy.read_bytes()==SOURCE
    result=publish(ctx);model=ctx.store.resolve(ctx.chat_id,result.models[0]['model'])
    receipt=publications.record_for(model)
    assert publications.availability(model)['status']=='available'
    monkeypatch.setattr(publications.time,'time',lambda:receipt['expires_at']+1)
    assert publications.availability(model)['status']=='needs_check'
    assert publications.approved(model) and model.read_bytes()==SOURCE


def test_cancelled_work_never_publishes_and_retry_resumes(setup,render_stub,monkeypatch):
    ctx,db,source,_=setup
    async def interrupted(*args): raise asyncio.CancelledError()
    monkeypatch.setattr(tools,'_trusted_review',interrupted)
    with pytest.raises(asyncio.CancelledError): publish(ctx)
    assert source.is_file() and not ctx.store.models(ctx.chat_id)
    monkeypatch.setattr(tools,'_trusted_review',render_stub)
    assert publish(ctx).models


@pytest.mark.skipif(sandbox._agent_account() is None,reason='Requires the real agent account')
def test_scripts_cannot_replace_collection_snapshot_or_receipts(setup):
    ctx,_,source,_=setup
    snapshot=publications.capture(SOURCE)
    code=f"from pathlib import Path\nfor p in {[str(settings.GENERATED_DIR/'bypass.mpd'),str(snapshot),str(snapshot.parent/'model.mpd') ]!r}:\n try: Path(p).write_text('0 changed'); print('BYPASS')\n except PermissionError: print('denied')"
    result=asyncio.run(sandbox.run(['python3','-c',code],ctx.work_dir,20))
    assert 'BYPASS' not in result.stdout and result.stdout.count('denied')==3
    result=asyncio.run(sandbox.run(['bash','-c',f'mv {snapshot.parent} {snapshot.parent}-replaced'],ctx.work_dir,20))
    assert result.exit_code!=0 and snapshot.read_bytes()==SOURCE


def test_strict_limits_missing_evidence_and_aggregated_alias_quantities(setup):
    _,db,_,_=setup
    req=[PartRequirement('3001','4',1)]
    now=time.time()
    for changes,reason in [({'max_quantity':None},'unknown_quantity_limit'),({'unit_price':None},'missing_price'),
                           ({'checked_at':now-601},'stale_observation'),({'checked_at':now+60},'stale_observation')]:
        catalogue={'300121':replace(db.elements['300121'],**changes)}
        quote=build_quote('a'*64,req,db.index,catalogue,strict=True,max_age=600,now=now)
        assert publication_problems(quote,now=now)[0]['issue']==reason
    db.index.parts['alias'].add('3001')
    quote=build_quote('a'*64,[PartRequirement('3001','4',60),PartRequirement('alias','4',60)],db.index,db.elements,strict=True,now=now)
    assert quote['coverage']==0 and all(p['issue']=='quantity_limit' for p in publication_problems(quote,now=now))


def test_discovery_never_widens_pool_on_typesafe_failure(setup):
    _,db,_,_=setup
    db.elements['300124']=replace(db.elements['300124'],availability='out_of_stock')
    groups=discovery.eligible(db.available_candidates())
    assert len(groups['3001']['choices'])==1 and groups['3001']['choices'][0]['color']=='4'
    def failure(*args,**kwargs): return type('Result',(),{'returncode':1})()
    ids,engine,_=discovery.rank(groups,'brick',key='private-test',runner=failure)
    assert ids==['3001'] and engine=='keyword'
    def injected(*args,**kwargs):
        return type('Result',(),{'returncode':0,'stdout':json.dumps({'results':[{'source':{'key':{'id':'unavailable'}}}]})})()
    assert discovery.rank(groups,'brick',key='private-test',runner=injected)[0]==['3001']
    assert discovery.eligible(db.available_candidates(),quantity=101)=={}
    assert not any('cart' in name for name in tools.TOOLS)


def test_expiry_during_database_save_does_not_expose_snapshot(setup,render_stub,monkeypatch):
    ctx,db,source,_=setup
    original=db.save_publication
    def delayed(record,quote,after):
        original(record,quote,after)
        monkeypatch.setattr(publications.time,'time',lambda:record['expires_at']+1)
    monkeypatch.setattr(db,'save_publication',delayed)
    assert not publish(ctx).models
    assert not list(settings.GENERATED_DIR.iterdir())
    assert not publications.artifact_allowed('model.mpd',source.read_bytes())


def test_lower_price_never_selects_insufficient_limit_for_combined_aliases(setup):
    _,db,_,_=setup
    db.index.parts['alias'].add('3001')
    db.index.elements['3001','4'].add('900001')
    db.elements['900001']=replace(db.elements['300121'],id='900001',unit_price=25,max_quantity=200)
    quote=build_quote('a'*64,[PartRequirement('3001','4',60),PartRequirement('alias','4',60)],db.index,db.elements,strict=True)
    assert quote['coverage']==1 and {l['element_id'] for l in quote['lines']}=={'900001'}


def test_failed_geometry_and_missing_database_startup_fail_closed(setup,monkeypatch):
    ctx,db,source,_=setup
    async def invalid(ctx,args,timeout):return sandbox.RunResult(1,'geometry failed','',False,0)
    monkeypatch.setattr(tools,'_trusted_review',invalid)
    assert not publish(ctx).models and not list(settings.GENERATED_DIR.iterdir())
    (publications._registry()/'installed').unlink()
    class Offline:
        def bootstrap_publications(self,legacy):raise RuntimeError('offline')
    with pytest.raises(RuntimeError,match='private database'):publications.initialize(Offline())
