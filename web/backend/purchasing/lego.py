"""Read-only shop adapter. This module contains no cart mutations or credentials."""
from __future__ import annotations

from dataclasses import asdict
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request

from .domain import CatalogueElement

ENDPOINT = 'https://www.lego.com/api/graphql'
QUERY = '''query PickABrickQuery($input: ElementQueryInput!) {
 searchElements(input: $input) { total count results {
  id designId name availability maxOrderQuantity deliveryChannel
  price { centAmount currencyCode } facets { color { key name } }
 } }
}'''
_limit = threading.Lock()
_last_request = 0.0


class CatalogueError(RuntimeError):
    pass


class LegoCatalogue:
    def __init__(self, *, interval=1.1, transport=None, sleep=time.sleep):
        self.interval, self.transport, self.sleep = interval, transport or self._request, sleep

    def _request(self, payload):
        global _last_request
        with _limit:
            self.sleep(max(0, self.interval - (time.monotonic() - _last_request)))
            _last_request = time.monotonic()
            request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
                'Content-Type': 'application/json', 'x-locale': 'en-US',
                'User-Agent': 'LDraw-Nova-private-catalogue/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)

    def page(self, *, page=1, ids=None, per_page=50):
        query_input = {'fetchSiblings': False, 'availability': ['AVAILABLE', 'OUT_OF_STOCK'],
                       'page': page, 'perPage': per_page,
                       'sort': {'direction': 'DESC', 'key': 'RELEVANCE'}}
        if ids is not None:
            if not ids or len(ids) > 100 or any(not str(i).isdecimal() for i in ids):
                raise ValueError('Use batches of 1 to 100 numeric element IDs')
            query_input.update(skus=list(ids), perPage=len(ids))
        for attempt in range(4):
            try:
                data = self.transport({'operationName': 'PickABrickQuery', 'query': QUERY,
                                       'variables': {'input': query_input}})
                break
            except urllib.error.HTTPError as error:
                # Challenges and permission failures must not be bypassed or retried.
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise CatalogueError(f'LEGO catalogue returned HTTP {error.code}') from None
                retry = error.headers.get('Retry-After', '')
                self.sleep(min(120, max(2 ** (attempt + 1), int(retry) if retry.isdecimal() else 0)) + random.random())
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise CatalogueError('LEGO catalogue connection failed') from None
                self.sleep(2 ** (attempt + 1))
        if data.get('errors'):
            raise CatalogueError('LEGO catalogue query changed or failed')
        try:
            result = data['data']['searchElements']
            total, count, rows = result['total'], result['count'], result['results']
            if type(total) is not int or type(count) is not int or count != len(rows) or total < count:
                raise ValueError('Invalid pagination')
            elements = []
            checked_at = time.time()
            for row in rows:
                price = row.get('price')
                elements.append(CatalogueElement(
                    id=str(row['id']), design_id=str(row['designId']),
                    color_id=str(row['facets']['color']['key']), name=row['name'],
                    availability={'AVAILABLE': 'available', 'OUT_OF_STOCK': 'out_of_stock'}[row['availability']],
                    unit_price=price['centAmount'] if price else None,
                    currency=price['currencyCode'] if price else 'USD', checked_at=checked_at,
                    max_quantity=row.get('maxOrderQuantity'), delivery_channel=row.get('deliveryChannel')))
            if len({e.id for e in elements}) != count:
                raise ValueError('Duplicate element IDs')
            if ids is not None and any(e.id not in ids for e in elements):
                raise ValueError('Unexpected element in a batch')
            return total, elements
        except (KeyError, TypeError, ValueError) as error:
            raise CatalogueError('LEGO catalogue response failed validation') from error

    def complete(self, checkpoint=lambda *args: None):
        expected, first = self.page()
        if expected < 1000 or expected > 100_000:
            raise CatalogueError('Unexpected catalogue size; keep the previous catalogue')
        all_elements = {e.id: e for e in first}
        checkpoint(1, len(all_elements), expected)
        for page in range(2, (expected + 49) // 50 + 1):
            total, elements = self.page(page=page)
            if total != expected or any(e.id in all_elements for e in elements):
                raise CatalogueError('Catalogue changed during pagination; keep the previous catalogue')
            all_elements.update({e.id: e for e in elements})
            checkpoint(page, len(all_elements), expected)
        if len(all_elements) != expected:
            raise CatalogueError('Incomplete catalogue; keep the previous catalogue')
        return all_elements

    def refresh(self, ids):
        output = {}
        ids = sorted(set(ids))
        for offset in range(0, len(ids), 100):
            batch = ids[offset:offset + 100]
            _, elements = self.page(ids=batch)
            output.update({e.id: e for e in elements})
            for missing in set(batch) - output.keys():
                output[missing] = CatalogueElement(missing, '', '', '', 'unlisted', None, time.time())
        return output

    def fee_policy(self):
        """Read the published fee rule, without inventing a checkout total.

        Existing bag contents and mixed delivery channels can change the actual fee.
        Fail closed if LEGO changes the published wording or its structured page.
        """
        url = 'https://www.lego.com/en-us/pick-and-build/pick-a-brick'
        with urllib.request.urlopen(url, timeout=30) as response:
            html = response.read(8 * 1024 * 1024).decode()
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not match: return []
        root = json.loads(match[1])['props']['pageProps']['__APOLLO_STATE__']['ROOT_QUERY']
        for key, messages in root.items():
            if not key.startswith('localization('): continue
            text = messages.get('pab.cart.info.serviceFeeCalulatedAtCheckout', '')
            amounts = re.fullmatch(r'A \$(\d+(?:\.\d{2})?) service fee applies to Bricks and Pieces orders under \$(\d+(?:\.\d{2})?)\. This will be calculated at checkout', text)
            if amounts:
                from decimal import Decimal
                amount, threshold = [int(Decimal(v) * 100) for v in amounts.groups()]
                return [{'name': f'Possible service fee when the LEGO parts bag is under ${threshold / 100:g}',
                         'amount': amount, 'threshold': threshold, 'conditional': True,
                         'checked_at': time.time(), 'source': url}]
        return []
