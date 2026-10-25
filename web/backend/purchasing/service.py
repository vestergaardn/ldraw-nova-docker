"""Revision pricing jobs; quotes are immutable and summaries are cheap to read."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
import time

import settings
from leocad_render import bom_path_for
from .database import Database, DatabaseUnavailable
from .domain import MAX_AGE, build_quote, requirements_from_csv, PartRequirement
from .lego import LegoCatalogue

log = logging.getLogger(__name__)
_loop = None
_tasks = {}
_cooldown = {}
_capacity = None
_interactive_capacity = None
_model_locks = {}


def initialize():
    global _loop, _capacity, _interactive_capacity
    _loop = asyncio.get_running_loop()
    _capacity = asyncio.Semaphore(2)
    _interactive_capacity = asyncio.Semaphore(1)


def revision_for(model):
    return hashlib.sha256(model.read_bytes()).hexdigest()


def _cache(revision):
    # /config is root-only. Agent tools cannot replace price or review data.
    return settings.CONFIG_DIR / 'pricing' / (revision + '.json')


def _save(quote):
    path = _cache(quote['revision'])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as stream:
        temporary=Path(stream.name); json.dump(quote,stream)
    temporary.chmod(0o600);temporary.replace(path)


def summary(model):
    if not model.is_file(): return None
    revision = revision_for(model)
    try:
        quote = json.loads(_cache(revision).read_text())
        result = {k: v for k, v in quote.items() if k != 'lines'}
        result['stale'] = quote['stale'] or time.time() - quote['created_at'] > MAX_AGE
        if result['stale']:schedule(model)
        return result
    except (FileNotFoundError, ValueError):
        schedule(model)
        return {'revision': revision, 'status': 'pending'}


def schedule(model):
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(_enqueue, model)


def _enqueue(model):
    key = str(model)
    if key in _tasks or len(_tasks)>=32 or time.time() - _cooldown.get(key, 0) < 60:
        return
    async def calculate():
        try:
            await quote_model(model, refresh=False, background=True)
            await quote_model(model, refresh=True, background=True)
        except Exception:
            log.exception('Could not update the model price')
        finally:
            _cooldown[key] = time.time()
    task = asyncio.create_task(calculate())
    _tasks[key] = task
    task.add_done_callback(lambda _: _tasks.pop(key, None))


def captured_requirements(source,revision):
    import toolkit
    folder=settings.CONFIG_DIR/'pricing'/'sources';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    cached=folder/(revision+'.json')
    if cached.is_file():return [PartRequirement(**r) for r in json.loads(cached.read_text())]
    snapshot=folder/(revision+'.mpd')
    snapshot.write_bytes(source);snapshot.chmod(0o600)
    environment=toolkit.environment()
    result=subprocess.run([str(toolkit.root()/'.venv/bin/python'),'-m','ldraw_tools.purchasing_bom',str(snapshot)],
                          env=environment,capture_output=True,text=True,timeout=120)
    if result.returncode:raise ValueError('The saved model could not produce a complete parts list')
    rows=json.loads(result.stdout);requirements=[PartRequirement(**r) for r in rows]
    if not requirements or any(r.quantity<1 for r in requirements):raise ValueError('Invalid model parts list')
    with tempfile.NamedTemporaryFile(mode='w',dir=folder,delete=False) as stream:
        temporary=Path(stream.name);json.dump(rows,stream)
    temporary.chmod(0o600);temporary.replace(cached)
    return requirements


async def quote_model(model: Path, *, refresh=False, database=None, provider=None, background=False):
    global _capacity, _interactive_capacity
    if _capacity is None:_capacity=asyncio.Semaphore(2)
    if _interactive_capacity is None:_interactive_capacity=asyncio.Semaphore(1)
    capacity=_capacity if background else _interactive_capacity
    # Each revision derives its own BOM from captured geometry. An editable sibling
    # CSV cannot change a purchase review or race with model regeneration.
    key=str(model.resolve())
    lock=_model_locks.setdefault(key,asyncio.Lock())
    async with capacity, lock:
        db, provider = database or Database(), provider or LegoCatalogue()
        source = await asyncio.to_thread(model.read_bytes)
        if len(source)>5*1024*1024:raise ValueError('Pricing supports model files up to 5 MB')
        revision = hashlib.sha256(source).hexdigest()
        requirements = await asyncio.to_thread(captured_requirements,source,revision)
        index, catalogue = await asyncio.to_thread(db.inputs, requirements)
        if refresh:
            ids = sorted({element for r in requirements for element in index.resolve(r)[0]})
            fresh = await asyncio.to_thread(provider.refresh, ids)
            await asyncio.to_thread(db.observe, fresh)
            catalogue.update(fresh)
        fees = await asyncio.to_thread(db.fees)
        quote = build_quote(revision, requirements, index, catalogue, fees=fees)
        await asyncio.to_thread(db.save_quote, quote, model.name, revision, requirements)
        await asyncio.to_thread(_save, quote)
        return quote


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks: task.cancel()
    if tasks: await asyncio.gather(*tasks, return_exceptions=True)
