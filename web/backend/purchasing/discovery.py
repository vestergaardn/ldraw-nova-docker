"""Rank only verified, purchasable candidates. Both engines use the same pool."""
from collections import defaultdict
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

import environment_config
import settings
import toolkit
from .database import Database
from .domain import CatalogueElement, MAX_AGE

_cache = None
_lock = threading.Lock()


def candidates(database=None):
    global _cache
    with _lock:
        if database is None and _cache and time.monotonic() - _cache[0] < 60:
            return _cache[1]
        rows = (database or Database()).available_candidates()
        if database is None:
            _cache = (time.monotonic(), rows)
        return rows


def eligible(rows, *, quantity=1, color=None, library=None, now=None, max_unit_price=None):
    now = time.time() if now is None else now
    library = Path(library or settings.LDRAW_DIR)
    groups, metadata = {}, {}
    color_text = (library / 'LDConfig.ldr').read_text() if (library / 'LDConfig.ldr').exists() else ''
    colors = {code: name.replace('_', ' ') for name, code in
              re.findall(r'0\s+!COLOUR\s+(\S+)\s+CODE\s+(\d+)', color_text)}
    for row in rows:
        part = row['part']
        if not re.fullmatch(r'[a-z0-9_.-]+', part) or '..' in part or (color is not None and row['color'] != color):
            continue
        element = CatalogueElement(**row['payload'])
        if (element.availability != 'available' or element.unit_price is None or element.max_quantity is None
                or (max_unit_price is not None and element.unit_price > max_unit_price)
                or quantity > element.max_quantity or not 0 <= now - element.checked_at <= MAX_AGE
                or (element.max_quantity_checked_at is not None and not 0 <= now-element.max_quantity_checked_at<=MAX_AGE)):
            continue
        if part not in metadata:
            file = library / 'parts' / (part + '.dat')
            text = file.read_text(encoding='utf-8-sig') if file.is_file() else ''
            org = re.search(r'^0\s+!LDRAW_ORG\s+(\S+)', text, re.M)
            title = text.splitlines()[0].removeprefix('0 ').strip() if text else ''
            metadata[part] = title if org and org[1] == 'Part' and not title.startswith(('~', '=', '_')) else None
        if metadata[part] is None:
            continue
        group = groups.setdefault(part, dict(part=part, name=metadata[part], choices=[]))
        choice = dict(color=row['color'], color_name=colors.get(row['color'], row['color']), element_id=element.id,
                      unit_price=element.unit_price, estimated_total=element.unit_price * quantity, max_quantity=element.max_quantity,
                      checked_at=element.checked_at, evidence=row['evidence'])
        if not any(c['color'] == choice['color'] and c['element_id'] == choice['element_id'] for c in group['choices']):
            group['choices'].append(choice)
    return groups


def rank(groups, query, *, limit=12, key=None, runner=subprocess.run):
    # The snapshot contains no unavailable record. Fallback cannot broaden it.
    root = settings.CONFIG_DIR / 'parts-search'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as folder:
        path = Path(folder) / 'candidates.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('create table candidates(id text primary key,full_description text not null)')
            db.execute('create virtual table search using fts5(id unindexed,full_description)')
            values = [(p, p + ' ' + row['name'] + ' ' + ' '.join(sorted({c['color_name'] for c in row['choices']})))
                      for p, row in groups.items()]
            db.executemany('insert into candidates values(?,?)', values)
            db.executemany('insert into search values(?,?)', values)
        reason = 'TypeSafe key is not configured'
        if key and groups:
            environment = toolkit.environment()
            environment['TYPESAFE_API_KEY'] = key
            executable = shutil.which('jev-rerank', path=environment['PATH'])
            if executable:
                try:
                    result = runner([executable, '--db', str(path), '--table-field', 'candidates.full_description',
                                     '--query', query, '--top', str(min(limit, len(groups))), '--candidates', '500',
                                     '--model', 'jev-1.13.0', '--json', '--show'],
                                    env=environment, cwd=folder, capture_output=True, text=True, timeout=60)
                    if result.returncode == 0:
                        data = json.loads(result.stdout)
                        ids = list(dict.fromkeys(r['source']['key']['id'] for r in data['results']))
                        if all(p in groups for p in ids):
                            return ids[:limit], 'typesafe', None
                    reason = 'TypeSafe search did not return a valid result'
                except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                    reason = 'TypeSafe search is temporarily unavailable'
            else:
                reason = 'TypeSafe search executable is unavailable'
        terms = re.findall(r'\w+', query.casefold())
        expression = ' OR '.join('"' + term + '"' for term in terms if term not in {'a','an','the','with','for','of','and','to','in','on'})
        if not expression:
            return [], 'keyword', reason
        with sqlite3.connect(path) as db:
            ids = [r[0] for r in db.execute('select id from search where search match ? order by rank,id limit ?', (expression, limit))]
        return ids, 'keyword', reason


def search(query, *, quantity=1, color=None, limit=12, max_unit_price=None, database=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError('Use a part role or part number, up to 1,000 characters')
    if type(quantity) is not int or quantity < 1 or type(limit) is not int or not 1 <= limit <= 40:
        raise ValueError('Use a positive required quantity and 1 to 40 results')
    if max_unit_price is not None and (type(max_unit_price) is not int or max_unit_price < 0):
        raise ValueError('Use a nonnegative maximum unit price in whole USD cents')
    groups = eligible(candidates(database), quantity=quantity, color=color, max_unit_price=max_unit_price)
    ids, engine, fallback = rank(groups, query, limit=limit, key=environment_config.snapshot().get('TYPESAFE_API_KEY'))
    return dict(query=query, engine=engine, fallback_reason=fallback, eligible_parts=len(groups),
                results=[groups[p] for p in ids], currency='USD', market='US',
                note='These exact part/colour choices have verified mappings and available stock observations. '
                     'Use check_build_parts on assemblies. Publication refreshes stock again; stock is not reserved.')


def alternatives(problems):
    groups = eligible(candidates())
    result = []
    for problem in problems[:50]:
        exact = groups.get(problem['part'])
        choices = [c for c in exact['choices'] if c['max_quantity'] >= problem['quantity']] if exact else []
        declared = {a['element_id'] for a in problem.get('alternatives', [])}
        variants = [dict(part=row['part'], name=row['name'], **choice)
                    for row in groups.values() for choice in row['choices']
                    if choice['element_id'] in declared and choice['max_quantity'] >= problem['quantity']]
        result.append(dict(part=problem['part'], color=problem['color'], available_colors=choices,
                           declared_variants=variants, requires_model_review=True,
                           search_query=problem.get('name') or problem['part']))
    return result
