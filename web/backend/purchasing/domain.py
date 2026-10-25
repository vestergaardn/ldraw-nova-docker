"""Pure purchasing rules. Money is integer USD cents; unknown is never zero."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import csv
import hashlib
import io
import json
import time
import uuid
from typing import TypedDict

class ModelQuote(TypedDict):
    id: str
    revision: str
    currency: str
    created_at: float
    lines: list[dict]
    known_subtotal: int
    subtotal: int | None
    total_quantity: int
    priced_quantity: int
    purchasable_quantity: int
    coverage: float
    unresolved_lines: int
    stale: bool
    fees: list[dict]
    fees_complete: bool
    review_hash: str


MAX_AGE = 26 * 3600
QUOTE_VERSION = 1


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class PartRequirement:
    part: str
    color: str
    quantity: int
    name: str = ''


@dataclass(frozen=True)
class CatalogueElement:
    id: str
    design_id: str
    color_id: str
    name: str
    availability: str
    unit_price: int | None
    checked_at: float
    max_quantity: int | None = None
    currency: str = 'USD'
    delivery_channel: str | None = None

    def __post_init__(self):
        if not self.id.isdecimal() or self.availability not in {'available', 'out_of_stock', 'unlisted'}:
            raise ValueError('Unexpected LEGO element identity or availability')
        if self.currency != 'USD' or (self.unit_price is not None and (type(self.unit_price) is not int or self.unit_price < 0)):
            raise ValueError('Expected a nonnegative USD price in cents')
        if self.max_quantity is not None and (type(self.max_quantity) is not int or self.max_quantity < 0):
            raise ValueError('Invalid element quantity limit')


@dataclass
class IdentityIndex:
    # Only import-direction mappings qualify. Export-only aliases can erase mould differences.
    parts: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    colors: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    lego_colors: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    elements: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    element_identity: dict[str, tuple[str, str]] = field(default_factory=dict)
    relations: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    evidence: str = ''

    def resolve(self, requirement: PartRequirement) -> tuple[list[str], str | None]:
        parts = self.parts.get(requirement.part, set())
        colors = self.colors.get(requirement.color, set())
        if len(parts) != 1:
            return [], 'ambiguous_part' if parts else 'unknown_part'
        if len(colors) != 1:
            return [], 'ambiguous_color' if colors else 'unknown_color'
        ids = sorted(self.elements.get((next(iter(parts)), next(iter(colors))), set()))
        return ids, None if ids else 'no_verified_element'


def requirements_from_csv(content: str) -> list[PartRequirement]:
    rows = csv.DictReader(io.StringIO(content))
    if not {'Part ID', 'Color Code', 'Quantity'} <= set(rows.fieldnames or []):
        raise ValueError('The model needs a verified parts list before pricing')
    counts, names = Counter(), {}
    for row in rows:
        part = row['Part ID'].strip().lower().removesuffix('.dat')
        color = row['Color Code'].strip()
        quantity = int(row['Quantity'])
        if not part or quantity < 1 or quantity > 100_000:
            raise ValueError('Invalid parts-list quantity')
        counts[part, color] += quantity
        names[part, color] = row.get('Part Name', '')
    if not counts or sum(counts.values()) > 100_000:
        raise ValueError('The parts list must contain 1 to 100,000 pieces')
    return [PartRequirement(p, c, n, names[p, c]) for (p, c), n in sorted(counts.items())]


def build_quote(revision: str, requirements: list[PartRequirement], index: IdentityIndex,
                catalogue: dict[str, CatalogueElement], *, now=None, fees=None) -> ModelQuote:
    now = time.time() if now is None else now
    lines = []
    for requirement in requirements:
        ids, reason = index.resolve(requirement)
        matches = [catalogue[i] for i in ids if i in catalogue]
        # A verified element-to-colour mapping is also checked against the shop's LEGO colour code.
        colors = index.colors.get(requirement.color, set())
        lego_colors = index.lego_colors.get(next(iter(colors)), set()) if len(colors) == 1 else set()
        matches = [e for e in matches if e.color_id in lego_colors or (e.availability=='unlisted' and not e.color_id)]
        matches.sort(key=lambda e: (e.availability != 'available', e.unit_price is None,
                                    e.unit_price if e.unit_price is not None else 0, e.id))
        chosen = matches[0] if matches else None
        state = chosen.availability if chosen else ('unlisted' if ids else 'unmapped')
        unit_price = chosen.unit_price if chosen else None
        stale = chosen is not None and now - chosen.checked_at > MAX_AGE
        limit = chosen.max_quantity if chosen else None
        issue = reason or ('catalogue_color_mismatch' if ids and not matches and any(i in catalogue for i in ids) else None)
        if chosen and limit is not None and requirement.quantity > limit:
            issue = 'quantity_limit'
        alternatives = []
        # Suggest only catalogue-declared mould/alternate relationships, in the exact
        # mapped colour. These remain proposals, never silently selected purchases.
        mapped_parts = index.parts.get(requirement.part, set())
        if len(mapped_parts) == 1 and len(colors) == 1 and (chosen is None or state != 'available'):
            for kind, candidate_part in index.relations.get(next(iter(mapped_parts)), []):
                if kind not in {'M', 'A'}: continue
                for candidate_id in sorted(index.elements.get((candidate_part, next(iter(colors))), set())):
                    candidate = catalogue.get(candidate_id)
                    if candidate and candidate.availability == 'available' and candidate.color_id in lego_colors:
                        alternatives.append({'part': candidate_part, 'element_id': candidate.id,
                            'unit_price': candidate.unit_price, 'relationship': 'mould_variant' if kind == 'M' else 'alternate',
                            'requires_model_review': True, 'evidence': index.evidence})
        lines.append({**asdict(requirement), 'element_id': chosen.id if chosen else None,
                      'candidate_ids': ids, 'state': state, 'unit_price': unit_price,
                      'line_total': unit_price * requirement.quantity if unit_price is not None else None,
                      'checked_at': chosen.checked_at if chosen else None, 'stale': stale,
                      'max_quantity': limit, 'issue': issue,
                      'evidence': index.evidence, 'alternatives': alternatives})
    grouped=Counter()
    for line in lines:
        if line['element_id']:grouped[line['element_id']]+=line['quantity']
    for line in lines:
        if line['element_id'] and line['max_quantity'] is not None and grouped[line['element_id']]>line['max_quantity']:
            line['issue']='quantity_limit'
    total = sum(r.quantity for r in requirements)
    known = sum(line['line_total'] for line in lines if line['line_total'] is not None)
    priced = sum(line['quantity'] for line in lines if line['unit_price'] is not None)
    purchasable = sum(line['quantity'] for line in lines if line['state'] == 'available'
                      and line['unit_price'] is not None and not line['stale'] and not line['issue'])
    return {'id': uuid.uuid4().hex, 'version': QUOTE_VERSION, 'revision': revision, 'currency': 'USD',
            'created_at': now, 'lines': lines, 'known_subtotal': known,
            'subtotal': known if priced == total else None, 'total_quantity': total,
            'priced_quantity': priced, 'purchasable_quantity': purchasable,
            'coverage': purchasable / total if total else 0,
            'unresolved_lines': sum(bool(l['issue']) or l['state'] != 'available' or l['unit_price'] is None for l in lines),
            'stale': any(l['stale'] for l in lines), 'fees': fees or [],
            'fees_complete': False, 'shipping_tax': 'calculated_at_checkout',
            'review_hash': digest(lines)}


def reviewed_items(quote: dict, *, available_only: bool, now=None) -> list[dict]:
    now = time.time() if now is None else now
    if now - quote['created_at'] > 600:
        raise ValueError('Refresh this quote before adding parts')
    valid = [l for l in quote['lines'] if l['state'] == 'available' and l['element_id']
             and l['unit_price'] is not None and not l['issue'] and l['checked_at'] is not None
             and now - l['checked_at'] <= 600]
    if len(valid) != len(quote['lines']) and not available_only:
        raise ValueError('Choose available parts only or resolve the unavailable parts')
    quantities = Counter()
    limits = {}
    for line in valid:
        quantities[line['element_id']] += line['quantity']
        limits[line['element_id']] = line['max_quantity']
    for element, quantity in quantities.items():
        if limits[element] is not None and quantity > limits[element]:
            raise ValueError('Combined quantities exceed a LEGO quantity limit')
    if not quantities:
        raise ValueError('No verified, available parts have fresh prices')
    return [{'elementId': e, 'quantity': q} for e, q in sorted(quantities.items())]


def import_csv(items: list[dict]) -> str:
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['elementId', 'quantity'])
    for item in items:
        if not str(item['elementId']).isdecimal() or type(item['quantity']) is not int or item['quantity'] < 1:
            raise ValueError('Invalid import item')
        writer.writerow([item['elementId'], item['quantity']])
    return output.getvalue()
