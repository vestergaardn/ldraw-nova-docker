"""Server-owned snapshots and publication receipts; never trust collection files alone."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile

import settings
from purchasing.database import Database
from purchasing.domain import PUBLICATION_MAX_AGE, publication_problems
from purchasing import service as pricing

log = logging.getLogger(__name__)
_commit_lock = None
MODEL_SUFFIXES = {'.mpd', '.ldr', '.dat'}


def _owned(path, mode, *, storage_root=False):
    if path.is_symlink():
        # Modal mounts a volume through a root-owned link. Permit only this
        # configured storage root, with a target and ancestors the agent cannot
        # replace. Model files, collection folders and snapshots still reject links.
        target = path.resolve(strict=True)
        protected = [path.parent.resolve(), *path.parent.resolve().parents,
                     target, *target.parents]
        if (not storage_root or path.lstat().st_uid != 0 or
                any(p.stat().st_uid != 0 or p.stat().st_mode & 0o022 for p in protected)):
            raise ValueError('A protected publication path cannot be an untrusted symbolic link')
    if os.geteuid() == 0:
        os.chown(path, 0, 0)
    path.chmod(mode)


def protect_collection():
    # Protect the parent too: write permission on /data would permit replacing
    # the entire generated directory even if generated itself were read-only.
    for path in (settings.DATA_DIR, settings.GENERATED_DIR):
        path.mkdir(parents=True, exist_ok=True)
        _owned(path, 0o755, storage_root=path == settings.DATA_DIR)
    for path in settings.GENERATED_DIR.iterdir():
        if path.is_file() and not path.is_symlink():
            _owned(path, 0o644)


def _registry():
    path = settings.CONFIG_DIR / 'publications'
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _owned(path, 0o700)
    return path


def _atomic_json(path, data):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream)
    temporary.chmod(0o600)
    temporary.replace(path)


def _record_path(name):
    return _registry() / (hashlib.sha256(name.encode()).hexdigest() + '.json')


def initialize(database=None):
    protect_collection()
    marker = _registry() / 'installed'
    legacy = [dict(file=p.name, revision=pricing.revision_for(p), enforced=False,
                   verification_quote=None, checked_at=None, expires_at=None)
              for p in settings.GENERATED_DIR.iterdir()
              if not p.is_symlink() and p.is_file() and p.suffix.lower() in MODEL_SUFFIXES]
    try:
        records = (database or Database()).bootstrap_publications(legacy)
    except Exception:
        if not marker.is_file():
            raise RuntimeError('Publication setup needs the private database. No new models can be published.') from None
        log.warning('Publication database unavailable; retaining saved receipts. New publication remains blocked.')
        return
    for record in records:
        _atomic_json(_record_path(record['file']), record)
    marker.write_text('generated-v1\n')
    marker.chmod(0o600)


def record_for(model):
    if model.is_symlink() or model.parent.resolve() != settings.GENERATED_DIR.resolve():
        return None
    try:
        record = json.loads(_record_path(model.name).read_text())
        if record['file'] == model.name and record['revision'] == pricing.revision_for(model):
            return record
    except (OSError, ValueError, KeyError):
        pass
    return None


def approved(model):
    return model.is_file() and record_for(model) is not None


def serving_allowed(path):
    if path.is_symlink() or path.parent.resolve() != settings.GENERATED_DIR.resolve():
        return False
    if path.suffix.lower() in MODEL_SUFFIXES:
        return approved(path)
    if path.suffix.lower() not in {'.png', '.csv', '.md', '.glb'}:
        return False
    return any(approved(path.with_suffix(suffix)) for suffix in MODEL_SUFFIXES)


def capture(source):
    """The agent can read this copy for review, but cannot write or replace it."""
    if not source or len(source) > 32 * 1024 * 1024:
        raise ValueError('Publication supports nonempty models up to 32 MB')
    revision = hashlib.sha256(source).hexdigest()
    parent = settings.DATA_DIR / '.publication-snapshots'
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _owned(parent, 0o755)
    folder = parent / revision
    folder.mkdir(mode=0o755, exist_ok=True)
    _owned(folder, 0o755)
    snapshot = folder / 'model.mpd'
    if snapshot.is_symlink():
        raise ValueError('Invalid snapshot')
    if not snapshot.exists():
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(source)
        temporary.chmod(0o444)
        temporary.replace(snapshot)
    if snapshot.read_bytes() != source:
        raise ValueError('The protected snapshot changed')
    _owned(snapshot, 0o444)
    return snapshot


def _reserve_name(slug):
    # Persist sequence numbers even when a database failure interrupts a commit.
    # Orphan receipts can never collide with a later publication attempt.
    path = _registry() / 'sequences'
    sequences = json.loads(path.read_text()) if path.exists() else {}
    pattern = re.compile(re.escape(slug) + r'-v(\d+)\.mpd')
    previous = [int(m[1]) for file in settings.GENERATED_DIR.iterdir() if (m := pattern.fullmatch(file.name))]
    version = max([sequences.get(slug, 0), *previous]) + 1
    sequences[slug] = version
    _atomic_json(path, sequences)
    return settings.GENERATED_DIR / f'{slug}-v{version}.mpd'


async def commit(snapshot, slug, quote, *, after, database=None):
    global _commit_lock
    if _commit_lock is None:
        _commit_lock = asyncio.Lock()
    async with _commit_lock:
        source = snapshot.read_bytes()
        revision = hashlib.sha256(source).hexdigest()
        if revision != quote['revision'] or publication_problems(quote, after=after):
            raise ValueError('Stock verification failed or expired. Repair and check the draft again.')
        target = _reserve_name(slug)
        checked = min(t for line in quote['lines'] for t in
                      (line['checked_at'], line.get('max_quantity_checked_at')) if t is not None)
        record = dict(file=target.name, revision=revision, enforced=True, policy_version=1,
                      verification_quote=quote['id'], checked_at=checked,
                      expires_at=checked + PUBLICATION_MAX_AGE)
        await asyncio.to_thread((database or Database()).save_publication, record, quote, after)
        # Persist privately before the visible rename. An interruption can leave
        # a receipt without a file, but never an approved file without evidence.
        _atomic_json(_record_path(target.name), record)
        with tempfile.NamedTemporaryFile(dir=settings.GENERATED_DIR, prefix='.pending-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(source)
        try:
            _owned(temporary, 0o644)
            if publication_problems(quote, after=after):
                raise ValueError('Stock verification expired while saving. Check the draft again.')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target, record


def availability(model):
    record = record_for(model) or dict(revision=pricing.revision_for(model), enforced=False,
                                     verification_quote=None, checked_at=None, expires_at=None)
    result = {key: record[key] for key in ('revision', 'enforced', 'verification_quote', 'checked_at', 'expires_at')}
    result['status'] = 'needs_check'
    try:
        quote = json.loads(pricing._cache(record['revision']).read_text())
        checked = [line.get('checked_at') for line in quote['lines']]
        if checked and all(t is not None for t in checked):
            checked += [line['max_quantity_checked_at'] for line in quote['lines'] if line.get('max_quantity_checked_at') is not None]
            result['checked_at'] = min(checked)
            result['expires_at'] = min(checked) + PUBLICATION_MAX_AGE
        if result['expires_at'] and time.time() < result['expires_at']:
            result['status'] = 'blocked' if publication_problems(quote) else 'available'
    except (OSError, ValueError, KeyError, TypeError):
        if record['enforced'] and time.time() < record['expires_at']:
            result['status'] = 'available'
    if str(model.resolve()) in pricing._tasks:
        result['status'] = 'checking'
    return result


def _approved_bytes(data):
    revision = hashlib.sha256(data).hexdigest()
    for path in _registry().glob('*.json'):
        record = json.loads(path.read_text())
        if record.get('revision') == revision and approved(settings.GENERATED_DIR / record['file']):
            return True
    return False


def artifact_allowed(name, data):
    """Inspect bytes once, then serve those same bytes to avoid draft edit races."""
    suffix = Path(name).suffix.lower()
    if suffix in MODEL_SUFFIXES or re.search(rb'(?m)^\s*1\s+\S+(?:\s+[-+\d.eE]+){12}\s+\S+', data):
        return _approved_bytes(data)
    # A renamed archive or binary model must not bypass the draft restriction.
    if data.startswith((b'PK\x03\x04', b'\x1f\x8b', b'glTF')) or suffix in {'.zip', '.tar', '.gz', '.glb', '.gltf', '.obj', '.stl'}:
        if suffix in {'.docx', '.xlsx', '.pptx'}:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    return all(not Path(entry.filename).suffix.lower() in MODEL_SUFFIXES
                               for entry in archive.infolist()) and '[Content_Types].xml' in archive.namelist()
            except zipfile.BadZipFile:
                return False
        return False
    return suffix in {'.md', '.txt', '.json', '.csv', '.py', '.sh', '.png', '.jpg', '.jpeg', '.webp', '.pdf'}
