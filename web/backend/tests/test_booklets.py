import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import booklets
import settings
import store as chat_store
from main import app


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch):
    # TestClient may initialise the singleton while OUTPUT_DIR is patched.
    monkeypatch.setattr(chat_store,'_store',None)


def test_job_snapshot_deduplicates_and_restart_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'OUTPUT_DIR', tmp_path)
    model = tmp_path / 'car.mpd'
    model.write_text('0 Original model\n')
    async def wait(*args):
        await asyncio.sleep(100)
    monkeypatch.setattr(booklets, '_generate', wait)
    async def run():
        first = await booklets.start(model)
        same = await booklets.start(model)
        assert first['id'] == same['id']
        model.write_text('0 Changed model\n')
        changed = await booklets.start(model)
        assert changed['id'] != first['id']
        assert (booklets.directory(first['id']) / 'source.mpd').read_text() == '0 Original model\n'
        await booklets.shutdown()
        await asyncio.sleep(0)
        assert booklets.status(first['id'])['status'] == 'failed'
    asyncio.run(run())


def test_pdf_api_checks_paths_and_only_serves_completed_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'OUTPUT_DIR', tmp_path)
    job = 'a' * 32
    folder = booklets.directory(job)
    folder.mkdir(parents=True)
    (folder / 'status.json').write_text(json.dumps(dict(id=job, status='failed', filename='car-guide.pdf')))
    with TestClient(app) as client:
        assert client.post('/api/assembly-guides', json={'url': '/files/generated/../../config/models.json'}).status_code == 404
        assert client.get('/api/assembly-guides/not-a-job/pdf').status_code == 404
        assert client.get(f'/api/assembly-guides/{job}/pdf').status_code == 409
        (folder / 'booklet.pdf').write_bytes(b'%PDF-1.7\n')
        response = client.get(f'/api/assembly-guides/{job}/pdf')
        assert response.status_code == 200
        assert response.headers['content-type'] == 'application/pdf'
        assert 'no-store' in response.headers['cache-control']
        assert 'car-guide.pdf' in response.headers['content-disposition']


def test_options_version_the_pdf_cache_but_share_completed_render_work(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'OUTPUT_DIR',tmp_path)
    model=tmp_path/'car.mpd';model.write_text('0 Model\n')
    async def wait(*args):await asyncio.sleep(100)
    monkeypatch.setattr(booklets,'_generate',wait)
    async def run():
        native=await booklets.start(model)
        letter=await booklets.start(model,print_format='letter')
        original=await booklets.start(model,improve=False)
        assert native['id']!=letter['id']!=original['id']
        assert native['render_key']==letter['render_key']!=original['render_key']
        await booklets.shutdown()
        monkeypatch.setattr(booklets,'VERSION',booklets.VERSION+':font-fix')
        revised=await booklets.start(model)
        assert revised['id']!=native['id'] and revised['render_key']==native['render_key']
        await booklets.shutdown()
    asyncio.run(run())


def test_running_job_exposes_saved_progress_and_elapsed_time(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'OUTPUT_DIR',tmp_path)
    model=tmp_path/'car.mpd';model.write_text('0 Model\n')
    async def wait(*args):await asyncio.sleep(100)
    monkeypatch.setattr(booklets,'_generate',wait)
    async def run():
        job=await booklets.start(model)
        folder=booklets.directory(job['id'])
        booklets._save(folder,{**job,'status':'rendering','started_at':job['created_at']})
        progress=booklets.root()/'render-cache'/job['render_key']/'progress.json'
        progress.parent.mkdir(parents=True)
        progress.write_text(json.dumps({'stage':'steps','completed':8,'total':30,'updated_at':job['created_at']}))
        response=TestClient(app).get('/api/assembly-guides/'+job['id'])
        result=response.json()
        assert result['progress']['completed']==8 and result['progress']['total']==30
        assert result['elapsed_seconds']>=0 and result['progress']['stage']=='steps'
        progress.write_text(json.dumps({'stage':'complete','completed':1,'total':1,'updated_at':job['created_at']-10}))
        assert 'progress' not in booklets.status(job['id']), 'A retry must not show a previous attempt as complete'
        progress.write_text('{interrupted write')
        assert booklets.status(job['id'])['status']=='rendering'
        await booklets.shutdown()
    asyncio.run(run())


def test_build_prepares_the_default_guide_and_cards_follow_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'OUTPUT_DIR',tmp_path)
    model=tmp_path/'car.mpd';model.write_text('0 Model\n')
    async def wait(*args):await asyncio.sleep(100)
    monkeypatch.setattr(booklets,'_generate',wait)
    async def run():
        assert booklets.prepared(model) is None
        await booklets.prepare(model)
        job=await booklets.start(model)
        assert booklets.prepared(model)==job['id'], 'The card must follow the job a request would reuse'
        booklets._save(booklets.directory(job['id']),{**job,'status':'failed'})
        await booklets.shutdown()
        assert booklets.prepared(model) is None, 'A failed background attempt keeps the create button'
        model.write_text('0 Edited model\n')
        assert booklets.prepared(model) is None
    asyncio.run(run())


def test_preparation_keeps_a_queue_slot_for_requested_guides(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'OUTPUT_DIR',tmp_path)
    async def wait(*args):await asyncio.sleep(100)
    monkeypatch.setattr(booklets,'_generate',wait)
    async def run():
        for number in range(booklets.MAX_PENDING-1):
            other=tmp_path/f'other{number}.mpd';other.write_text(f'0 Other {number}\n')
            await booklets.start(other)
        model=tmp_path/'car.mpd';model.write_text('0 Model\n')
        await booklets.prepare(model)
        assert booklets.prepared(model) is None
        assert (await booklets.start(model))['status']=='queued'
        await booklets.shutdown()
    asyncio.run(run())


def test_turn_prepares_only_its_final_published_model(tmp_path, monkeypatch):
    import agent
    monkeypatch.setattr(settings,'OUTPUT_DIR',tmp_path)
    monkeypatch.setattr(settings,'GENERATED_DIR',tmp_path/'generated')
    store=chat_store.get_store()
    chat=store.create_chat()
    (tmp_path/'generated').mkdir()
    paths=[tmp_path/'generated'/f'car-v{number}.mpd' for number in (1,2,3)]
    for path in paths:path.write_text(f'0 {path.stem}\n')
    store.add_model(chat['id'],'old',paths[0],[])
    time.sleep(0.01)
    since=time.time()
    for path in paths[1:]:
        store.add_model(chat['id'],path.stem,path,[])
    started=[]
    async def prepare(model):started.append(model)
    monkeypatch.setattr(booklets,'prepare',prepare)
    asyncio.run(agent.prepare_guide(store,chat['id'],since))
    assert started==[store.resolve(chat['id'],store.models(chat['id'])[-1]['model'])]
    started.clear()
    asyncio.run(agent.prepare_guide(store,chat['id'],time.time()+60))
    assert started==[]
