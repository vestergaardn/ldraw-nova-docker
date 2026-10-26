"""Real privilege boundary for server ranking and builder reference search."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import pytest

import sandbox
import settings
import toolkit
from purchasing import discovery


def test_server_ranking_cannot_poison_builder_cache(monkeypatch, tmp_path):
    os.chmod(tmp_path, 0o755)
    # pytest's parents need traversal by the sandbox user.
    for parent in list(tmp_path.parents)[:2]:
        os.chmod(parent, 0o755)
    shared = tmp_path / 'builder-cache'
    shared.mkdir()
    sandbox.give_to_agent(shared)
    original = toolkit.environment()
    monkeypatch.setattr(toolkit, 'environment', lambda: {**original, 'XDG_CACHE_HOME': str(shared)})
    monkeypatch.setattr(settings, 'CONFIG_DIR', tmp_path / 'private')
    seen = []
    def runner(argv, **kwargs):
        seen.append(kwargs['env']['XDG_CACHE_HOME'])
        # Exercise the real CLI/index without making an external request.
        kwargs['env']['TYPESAFE_BASE_URL'] = 'http://127.0.0.1:1'
        return subprocess.run([*argv, '--timeout', '0.1', '--retries', '0'], **kwargs)
    groups = {'3001': {'name': 'Brick 2 x 4', 'choices': [{'color_name': 'Red'}]}}
    discovery.rank(groups, 'brick', key='fixture-only', runner=runner)
    assert len(seen) == 1
    assert Path(seen[0]).is_relative_to(settings.CONFIG_DIR), 'Server ranking used the builder cache'
    assert not (shared / 'jev-rerank').exists(), 'Root-owned index blocks the next builder search'


def test_shared_preparation_survives_one_waiter_stop_and_reuses_success():
    async def check():
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def prepare():
            calls.append(1)
            started.set()
            await finish.wait()
        preparation = toolkit.Preparation(prepare)
        first = asyncio.create_task(preparation.wait())
        await started.wait()
        second = asyncio.create_task(preparation.wait())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not preparation.task.cancelled()
        finish.set()
        await second
        await preparation.wait()
        assert len(calls) == 1
        await preparation.close()
    asyncio.run(check())


def test_preparation_failure_can_retry_and_shutdown_stops_subprocess():
    async def check():
        calls = []
        async def prepare():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError('index unavailable')
            await asyncio.Future()
        preparation = toolkit.Preparation(prepare)
        with pytest.raises(ValueError, match='index unavailable'):
            await preparation.wait()
        task = preparation.start()
        await asyncio.sleep(0)
        assert len(calls) == 2
        await preparation.close()
        assert task.cancelled()
    asyncio.run(check())


def test_real_preparation_uses_agent_and_leaves_index_readable(monkeypatch):
    os.chmod(settings.DATA_DIR.parent, 0o755)
    captured = []
    original = sandbox.run
    async def run(argv, cwd, timeout, **kwargs):
        captured.append((argv, cwd, timeout, kwargs))
        return await original(argv, cwd, timeout, **kwargs)
    monkeypatch.setattr(sandbox, 'run', run)
    asyncio.run(toolkit._prepare())
    assert captured[0][2] == 900
    assert captured[0][3]['trusted'] is False
    assert captured[1][3]['trusted'] is True
    assert captured[0][3]['env_extra']['LDRAW_NOVA_CACHE_DIR'] != captured[1][3]['env_extra']['LDRAW_NOVA_CACHE_DIR']
    assert 'TYPESAFE_API_KEY' not in captured[0][3]['env_extra']
    index = next(Path(toolkit.environment()['LDRAW_NOVA_CACHE_DIR']).glob('library-*/parts.lst'))
    assert index.stat().st_size > 100_000
    assert index.stat().st_uid == sandbox._agent_account().pw_uid
    private_index = next(Path(toolkit.server_environment()['LDRAW_NOVA_CACHE_DIR']).glob('library-*/parts.lst'))
    denied = asyncio.run(sandbox.run(['test', '-r', str(private_index)], captured[0][1], 10))
    assert denied.exit_code != 0, 'Builder code can read the trusted validation cache'

