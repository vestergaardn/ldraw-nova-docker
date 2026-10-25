"""Import the official daily bulk files. Never crawl Rebrickable pages."""
import csv
import gzip
import hashlib
from pathlib import Path

from .domain import IdentityIndex

REQUIRED = ('parts', 'colors', 'elements', 'part_mappings', 'color_mappings', 'part_relationships')


def load(directory: Path) -> IdentityIndex:
    index = IdentityIndex()
    hashes = {}
    def rows(name):
        path = directory / (name + '.csv.gz')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        with gzip.open(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            yield from csv.DictReader(stream)
    # Validate the source identities before trusting their alias/element rows.
    parts = {r['part_num'] for r in rows('parts')}
    colors = {r['id'] for r in rows('colors')}
    for row in rows('part_mappings'):
        if row['ext_system'] == 'LD' and row['is_import_id'] == 'True' and row['part_num'] in parts:
            index.parts[row['ext_id'].lower().removesuffix('.dat')].add(row['part_num'])
    for row in rows('color_mappings'):
        if row['id'] not in colors:
            continue
        if row['ext_system'] == 'LD' and row['is_import_id'] == 'True':
            index.colors[row['ext_id']].add(row['id'])
        if row['ext_system'] == 'LE':
            index.lego_colors[row['id']].add(row['ext_id'])
    for row in rows('elements'):
        if row['part_num'] not in parts or row['color_id'] not in colors or not row['element_id'].isdecimal():
            continue
        key = row['part_num'], row['color_id']
        element = row['element_id']
        if element in index.element_identity and index.element_identity[element] != key:
            raise ValueError('Conflicting Rebrickable element identities')
        index.element_identity[element] = key
        index.elements[key].add(element)
    for row in rows('part_relationships'):
        # Relationship records are evidence for review, never automatic substitutions.
        index.relations[row['parent_part_num']].append((row['rel_type'], row['child_part_num']))
    index.evidence = 'Rebrickable bulk; sha256=' + hashlib.sha256(str(sorted(hashes.items())).encode()).hexdigest()
    return index
