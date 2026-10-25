from fastapi.testclient import TestClient
from main import app
import tools


def test_purchasing_keeps_private_origin_checks_and_model_path_boundaries():
    with TestClient(app) as client:
        for path in ['/api/quotes','/api/cart-intents']:
            response=client.post(path,json={},headers={'origin':'https://unapproved.example','sec-fetch-site':'cross-site'})
            assert response.status_code==403
        assert client.post('/api/quotes',json={'url':'/files/generated/../../config/secret.mpd'}).status_code==404
        assert client.get('/api/quotes/summary',params={'url':'https://example.com/model.mpd'}).status_code==404
        assert client.post('/api/assembly-guides',json={'url':'/files/generated/model.mpd','template':'untrusted'}).status_code==422
    # Generation can publish and price models; it has no cart-action tool.
    assert not any('cart' in schema['function']['name'] for schema in tools.TOOL_SCHEMAS)


def test_helper_is_a_local_package_with_narrow_permissions():
    import io,json,zipfile
    with TestClient(app) as client:
        response=client.get('/api/chrome-helper/download')
        assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest=json.loads(archive.read('lego-nova-helper/manifest.json'))
        assert manifest['permissions']==['storage']
        assert set(manifest['host_permissions'])=={'https://lego-nova.vercel.app/*','https://www.lego.com/*'}
        assert not any('test' in name or '..' in name for name in archive.namelist())


def test_user_review_is_not_queued_behind_background_pricing(tmp_path,monkeypatch):
    import asyncio
    from purchasing import service
    from purchasing.domain import IdentityIndex,PartRequirement
    model=tmp_path/'model.mpd';model.write_text('0 fixture')
    index=IdentityIndex()
    class Database:
        def inputs(self,requirements):return index,{}
        def fees(self):return None
        def save_quote(self,*args):pass
    monkeypatch.setattr(service,'captured_requirements',lambda *args:[PartRequirement('unknown','4',1)])
    monkeypatch.setattr(service,'_save',lambda quote:None)
    monkeypatch.setattr(service,'_model_locks',{})
    async def check():
        monkeypatch.setattr(service,'_capacity',asyncio.Semaphore(0))
        monkeypatch.setattr(service,'_interactive_capacity',asyncio.Semaphore(1))
        queued=asyncio.create_task(service.quote_model(model,database=Database(),background=True))
        await asyncio.sleep(0)
        quote=await asyncio.wait_for(service.quote_model(model,database=Database()),timeout=2)
        assert quote['total_quantity']==1 and not queued.done()
        queued.cancel()
        await asyncio.gather(queued,return_exceptions=True)
    asyncio.run(check())


def test_summary_reports_price_observation_age_and_refresh_progress(tmp_path,monkeypatch):
    import json,time
    from purchasing import service
    model=tmp_path/'revision.mpd';model.write_text('0 model')
    monkeypatch.setattr(service.settings,'CONFIG_DIR',tmp_path/'config')
    revision=service.revision_for(model);now=time.time()
    cache=service._cache(revision);cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({'id':'saved','revision':revision,'created_at':now,'stale':False,
                                'lines':[{'checked_at':now-3600},{'checked_at':now-10}]}))
    running={str(model.resolve()):object()};monkeypatch.setattr(service,'_tasks',running)
    summary=service.summary(model)
    assert summary['checked_at']==now-3600 and summary['refreshing'] is True
    running.clear()
    assert service.summary(model)['refreshing'] is False
