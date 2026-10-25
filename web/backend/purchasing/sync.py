"""Daily bulk imports, with whole-catalogue validation and atomic promotion."""
from datetime import datetime, timezone
import hashlib
import json
import shutil
from pathlib import Path
import time
import urllib.request

from .database import Database
from .lego import LegoCatalogue
from .rebrickable import REQUIRED, load

BULK_BASE = 'https://cdn.rebrickable.com/media/downloads/'


def identity_files(root: Path):
    # Official downloads permit automated bulk retrieval at most once each day.
    date = datetime.now(timezone.utc).date().isoformat()
    folder = root / date
    folder.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED:
        target = folder / (name + '.csv.gz')
        if target.exists(): continue
        request = urllib.request.Request(BULK_BASE + target.name,
                                         headers={'User-Agent': 'LDraw-Nova-private-catalogue/1.0'})
        with urllib.request.urlopen(request, timeout=90) as response:
            content = response.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise ValueError('Unexpectedly large identity file')
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(content)
        temporary.replace(target)
        time.sleep(1.1)
    return folder


def run(root: Path, *, database=None, provider=None):
    db, provider = database or Database(), provider or LegoCatalogue()
    run_id = db.start_run()
    try:
        folder = identity_files(root)
        index = load(folder)
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.csv.gz')}
        identity = db.import_identities(index, hashes)
        elements = provider.complete()
        fees = provider.fee_policy()
        db.promote(run_id, elements, identity, fees)
        for old in sorted(p for p in root.iterdir() if p.is_dir() and len(p.name)==10)[:-7]:
            shutil.rmtree(old)
        return {'run': run_id, 'elements': len(elements), 'identity': identity, 'status': 'complete'}
    except Exception as exc:
        db.fail_run(run_id, exc)
        raise
