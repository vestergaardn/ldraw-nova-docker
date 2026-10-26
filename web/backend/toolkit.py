"""Container integration for the unchanged, standalone ldraw-nova checkout."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from pathlib import Path

import settings
import sandbox
import environment_config


# Mandatory, category-independent foundations. Supply these in full before the
# first model call; partial tool reads were skipping design and review rules.
# Subject-specific guides and the CLI reference remain available through tools.
BUILDER_GUIDES = (
    "docs/agent/ldraw-reference.md",
    "docs/agent/geometry.md",
    "docs/agent/visual-design.md",
    "docs/agent/reference-discovery.md",
    "docs/agent/validation.md",
)


def root() -> Path:
    path = settings.TOOLKIT_DIR
    if not (path / "instructions.md").is_file() or not (path / ".venv/bin/python").exists():
        raise ValueError("The model-building toolkit is missing. Rebuild the server with its companion toolkit checkout.")
    return path


def workspace(store, chat_id: str) -> Path:
    """A repository-shaped cwd; source resources stay shared and read-only.

    ROOT in the upstream Python package resolves to the installed checkout,
    preserving its data paths and cache. Relative output paths are chat-local.
    """
    source = root()
    cwd = store.chat_dir(chat_id) / "workspace"
    cwd.mkdir(parents=True, exist_ok=True)
    output = store.work_dir(chat_id)
    output.mkdir(parents=True, exist_ok=True)
    sandbox.give_to_agent(output)
    for entry in source.iterdir():
        if entry.name in {"output", ".git", ".env"} or entry.name.startswith(".env."):
            continue
        link = cwd / entry.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(entry, target_is_directory=entry.is_dir())
    link = cwd / "output"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(output, target_is_directory=True)
    return cwd


def environment() -> dict[str, str]:
    source = root()
    env = environment_config.snapshot()
    return {
        "PATH": f"{source / '.venv/bin'}:{sandbox.SCRIPTS_DIR}:/usr/local/bin:/usr/bin:/bin",
        # Avoid the backend's old ldraw.py shadowing pyldraw3's `ldraw` package.
        "PYTHONPATH": f"{source}:/app",
        # Rosetta can create a root-owned ~/.cache before privileges drop on
        # Apple Silicon. Use the builder's explicitly writable derived cache.
        "XDG_CACHE_HOME": str(source / ".cache" / "builder"),
        "LDRAW_NOVA_CACHE_DIR": str(source / ".cache" / "builder"),
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "LDRAW_DIR": env.get("LDRAW_DIR") or env.get("LDRAWDIR") or str(settings.LDRAW_DIR),
    }


def server_environment() -> dict[str, str]:
    """Trusted validation must not read indexes controlled by the building AI."""
    cache = root() / '.server-cache'
    return {**environment(), 'XDG_CACHE_HOME': str(cache), 'LDRAW_NOVA_CACHE_DIR': str(cache),
            'LDRAW_DIR': str(settings.LDRAW_DIR)}


def instructions() -> str:
    return (root() / "instructions.md").read_text()


def builder_guides() -> str:
    source = root()
    return "\n\n".join(f"--- {path} (complete) ---\n{(source / path).read_text()}" for path in BUILDER_GUIDES)


async def _prepare() -> None:
    """Warm the full library once, outside the building AI's command deadlines."""
    source = root()
    cwd = settings.OUTPUT_DIR / '.builder-setup'
    env = environment()
    # The subprocess has the same identity and paths as actual builder commands.
    # No keys or database credentials are needed to initialize the local library.
    for environment_vars, trusted in ((env, False), (server_environment(), True)):
        # Give trusted commands a different working directory too. The server
        # never follows an agent-created index or working-directory symlink.
        work = settings.CONFIG_DIR / 'tool-setup' if trusted else cwd
        if trusted:
            cache = Path(environment_vars['LDRAW_NOVA_CACHE_DIR'])
            cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache.chmod(0o700)
        result = await sandbox.run([str(source / '.venv/bin/python'), '-c',
            'from ldraw_tools.common import get_parts, database_path; import sqlite3; get_parts(); '
            'db=sqlite3.connect(database_path().as_uri()+"?mode=ro", uri=True); '
            'db.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone(); db.close(); '
            'print("Parts index and reference database ready", flush=True)'],
            work, 900, env_extra=environment_vars, trusted=trusted)
        if result.exit_code != 0 or result.timed_out:
            logging.getLogger('toolkit').error('Parts setup failed: %s', result.as_text())
            raise ValueError('The LEGO parts index could not initialize. Your draft is saved. Retry the build after setup recovers.')


class Preparation:
    """One shared, cancellable worker task; stopping one chat cannot cancel it."""
    def __init__(self, prepare):
        self.prepare = prepare
        self.task = None

    def start(self):
        if self.task is None or self.task.cancelled() or (self.task.done() and self.task.exception() is not None):
            self.task = asyncio.create_task(self.prepare())
            # Retrieve background failures even when no chat is waiting yet.
            self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return self.task

    async def wait(self):
        await asyncio.shield(self.start())

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.task = None


_preparation = Preparation(_prepare)


def start_preparation():
    return _preparation.start()


async def wait_ready():
    await _preparation.wait()


async def shutdown():
    await _preparation.close()
