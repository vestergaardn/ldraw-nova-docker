"""Bounded, deduplicated assembly-guide jobs with durable PDF downloads."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import time

import render
import sandbox
import settings
import toolkit

VERSION = '2:creator-31134-v1:plan-4:outfit-1'
TIMEOUT = 3500
MAX_PENDING = 4
_tasks: dict[str, asyncio.Task] = {}
_models: dict[str, Path] = {}
log = logging.getLogger(__name__)


def root():
    return settings.OUTPUT_DIR / 'assembly-guides'


def directory(job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Unknown assembly guide')
    return root() / job_id


def _save(folder, data):
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / 'status.tmp'
    temporary.write_text(json.dumps(data))
    temporary.replace(folder / 'status.json')


def status(job_id):
    folder = directory(job_id)
    if not (folder / 'status.json').is_file():
        raise FileNotFoundError('Unknown assembly guide')
    data = json.loads((folder / 'status.json').read_text())
    if (folder / 'booklet.pdf').is_file():
        data.update(status='ready', download_url=f'/api/assembly-guides/{job_id}/pdf')
    elif data['status'] in ('queued', 'rendering') and job_id not in _tasks:
        data.update(status='failed', error='Generation stopped when the server restarted. Please try again.')
    return data


def is_busy(model):
    return model.resolve() in _models.values()


async def start(model, *, template='creator-31134', improve=True, print_format='native'):
    if template not in {'creator-31134','city-clear'} or print_format not in {'native','letter'} or not isinstance(improve,bool):
        raise ValueError('Invalid guide options')
    options=dict(template=template,improve=improve,print_format=print_format)
    content = await asyncio.to_thread(model.read_bytes)
    if len(content) > 5 * 1024 * 1024:
        raise ValueError('Choose a model smaller than 5 MB for an assembly guide')
    render_key = hashlib.sha256(VERSION.encode() + str(model.resolve()).encode() + json.dumps(dict(template=template,improve=improve),sort_keys=True).encode() + content).hexdigest()[:32]
    key = hashlib.sha256((render_key+print_format).encode()).hexdigest()[:32]
    folder = directory(key)
    if key in _tasks or (folder / 'booklet.pdf').is_file():
        return status(key)
    if len(_tasks) >= MAX_PENDING:
        raise RuntimeError('The guide queue is full. Try again when a guide finishes.')
    folder.mkdir(parents=True, exist_ok=True)
    sandbox.give_to_agent(folder)
    # Snapshot before queuing. A model edited during rendering must not change
    # the cached PDF or produce a guide under the wrong content hash.
    source = folder / ('source' + model.suffix.lower())
    source.write_bytes(content)
    data = dict(id=key, status='queued', filename=model.stem + '-assembly-guide.pdf',
                created_at=time.time(), error=None, options=options, render_key=render_key)
    _save(folder, data)
    _models[key] = model.resolve()
    task = asyncio.create_task(_generate(key, model, source, data))
    _tasks[key] = task
    def done(_task):
        _tasks.pop(key, None)
        _models.pop(key, None)
    task.add_done_callback(done)
    return data


async def _generate(key, model, source, data):
    folder = directory(key)
    try:
        # Share the renderer lock with snapshots and BOM generation.
        async with render._rendering(model):
            data['status'] = 'rendering'
            _save(folder, data)
            sandbox.give_to_agent(root())
            result = await sandbox.run(
                [str(toolkit.root() / 'ldraw-agent'), 'manual', 'pdf', str(source),
                 '--output', str(folder / 'booklet.pdf'), '--template', data['options']['template'],
                 '--print-format', data['options']['print_format'],
                 '--workdir', str(root() / 'render-cache' / data['render_key']),
                 *([] if data['options']['improve'] else ['--no-improve'])],
                cwd=folder, timeout=TIMEOUT, env_extra=toolkit.environment())
            if result.exit_code != 0 or not (folder / 'booklet.pdf').is_file():
                details = result.stderr.strip() or result.stdout.strip()
                log.warning('Assembly guide %s failed: %s', key, details[-2000:])
                if result.timed_out:
                    raise ValueError('Rendering paused at the time limit. Retry to resume completed work.')
                # CLI errors are JSON; expose its actionable message, not logs or paths.
                try:
                    error = json.loads(result.stdout).get('error', '')
                except (ValueError, AttributeError):
                    error = ''
                raise ValueError(error or 'Could not create this guide. Check the model has valid building steps.')
            report = json.loads(result.stdout)
            data.update(status='ready', pages=report['pages'], parts=report['parts'], steps=report['steps'],
                        native_pages=report.get('native_pages'), fidelity=report.get('fidelity'),
                        geometry_preserved=report.get('geometry_preserved'),font=report.get('font'))
            _save(folder, data)
    except asyncio.CancelledError:
        data.update(status='failed', error='Generation stopped. Please try again.')
        _save(folder, data)
        raise
    except Exception as exc:
        log.exception('Assembly guide failed: %s', key)
        data.update(status='failed', error=str(exc) if isinstance(exc, ValueError) else 'Could not create this guide. Please try again.')
        _save(folder, data)


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
