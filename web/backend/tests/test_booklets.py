import asyncio
import json
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
