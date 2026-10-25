"""A dedicated Postgres connection. No credentials ever reach the agent or browser."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
import secrets
import time
import uuid

from .domain import CatalogueElement, IdentityIndex, reviewed_items, publication_problems


class DatabaseUnavailable(RuntimeError):
    pass


@contextmanager
def copy_rows(db, table, columns):
    """COPY into a temporary table, then INSERT under the real table's RLS policy."""
    from psycopg import sql
    staging = sql.Identifier('staging_' + table)
    target = sql.Identifier('lego', table)
    fields = sql.SQL(',').join(map(sql.Identifier, columns))
    db.execute(sql.SQL('create temporary table {} (like {}) on commit drop').format(staging, target))
    with db.cursor().copy(sql.SQL('copy {} ({}) from stdin').format(staging, fields)) as copy:
        yield copy
    db.execute(sql.SQL('insert into {} ({}) select {} from {}').format(target, fields, fields, staging))


class Database:
    def __init__(self, url=None):
        self.url = url or os.environ.get('LEGO_DATABASE_URL')

    @contextmanager
    def connection(self):
        if not self.url:
            raise DatabaseUnavailable('Parts pricing is waiting for its database connection')
        import psycopg
        from psycopg.rows import dict_row
        try:
            connection = psycopg.connect(self.url, connect_timeout=10, row_factory=dict_row)
        except psycopg.Error:
            raise DatabaseUnavailable('The parts database is temporarily unavailable') from None
        with connection:
            # Poolers may ignore startup options. Enforce bounds in this transaction,
            # including abandoned clients after a container is replaced.
            connection.execute("set local statement_timeout='30s'")
            connection.execute("set local idle_in_transaction_session_timeout='30s'")
            connection.execute("set local lock_timeout='5s'")
            yield connection

    def import_identities(self, index, hashes):
        identity = hashlib.sha256(index.evidence.encode()).hexdigest()
        with self.connection() as db:
            if db.execute('select 1 from lego.identity_imports where id=%s and exists(select 1 from lego.element_identities where import_id=lego.identity_imports.id)', (identity,)).fetchone():
                return identity
            db.execute('insert into lego.identity_imports(id,source,hashes) values(%s,%s,%s::jsonb) on conflict(id) do nothing',
                       (identity, index.evidence, json.dumps(hashes)))
            with copy_rows(db, 'part_aliases', ['import_id','ldraw_part','part_num']) as copy:
                for external, parts in index.parts.items():
                    for part in parts: copy.write_row((identity, external, part))
            with copy_rows(db, 'color_mappings', ['import_id','system','external_id','color_id']) as copy:
                for external, colors in index.colors.items():
                    for color in colors: copy.write_row((identity, 'LD', external, color))
                for color, external_ids in index.lego_colors.items():
                    for external in external_ids: copy.write_row((identity, 'LE', external, color))
            with copy_rows(db, 'element_identities', ['import_id','element_id','part_num','color_id']) as copy:
                for element, (part, color) in index.element_identity.items():
                    copy.write_row((identity, element, part, color))
            with copy_rows(db, 'part_relationships', ['import_id','parent_part','child_part','kind']) as copy:
                for parent, relations in index.relations.items():
                    for kind, child in set(relations): copy.write_row((identity, parent, child, kind))
        return identity

    def start_run(self):
        run = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("insert into lego.catalogue_runs(id,status) values(%s,'running')", (run,))
        return run

    def fail_run(self, run, error):
        with self.connection() as db:
            db.execute("update lego.catalogue_runs set status='failed',finished_at=now(),error=%s where id=%s and status='running'",
                       (str(error)[:500], run))

    def promote(self, run, elements, identity_id=None, fees=None):
        if len(elements) < 1000:
            raise ValueError('Refusing an incomplete catalogue')
        with self.connection() as db:
            # Serialize promotions so overlapping scheduled/manual runs cannot interleave.
            db.execute('select pg_advisory_xact_lock(3113401)')
            old = db.execute("select run_id, identity_import_id from lego.current_catalogue where market='US'").fetchone()
            current_run = db.execute('select status from lego.catalogue_runs where id=%s for update', (run,)).fetchone()
            if not current_run or current_run['status'] != 'running':
                raise ValueError('Catalogue run is no longer active')
            with copy_rows(db, 'catalogue_elements', ['run_id','element_id','payload']) as copy:
                for element in elements.values():
                    copy.write_row((run, element.id, json.dumps(asdict(element))))
            if old:
                db.execute('''insert into lego.price_changes(element_id,old_value,new_value)
                    select n.element_id,o.payload,n.payload from lego.catalogue_elements n
                    left join lego.catalogue_elements o on o.run_id=%s and o.element_id=n.element_id
                    where n.run_id=%s and (o.payload->'unit_price',o.payload->'availability')
                       is distinct from (n.payload->'unit_price',n.payload->'availability')''', (old['run_id'], run))
            db.execute("update lego.catalogue_runs set status='complete',finished_at=now(),actual_count=%s,expected_count=%s,fee_policy=%s::jsonb where id=%s",
                       (len(elements), len(elements), json.dumps(fees or []), run))
            db.execute('''insert into lego.current_catalogue(market,run_id,identity_import_id) values('US',%s,%s)
                       on conflict(market) do update set run_id=excluded.run_id,identity_import_id=excluded.identity_import_id''',
                       (run, identity_id or (old['identity_import_id'] if old else None)))
            # Keep the previous complete snapshot for recovery. Quotes and price changes
            # retain historical values without storing a full catalogue every day forever.
            keep = [run] + ([str(old['run_id'])] if old else [])
            db.execute('delete from lego.catalogue_elements where not (run_id=any(%s::uuid[]))', (keep,))
            identities=list({x for x in [identity_id,old['identity_import_id'] if old else None] if x})
            if identities:
                from psycopg import sql
                for table in ['part_aliases','color_mappings','element_identities','part_relationships']:
                    db.execute(sql.SQL('delete from lego.{} where not (import_id=any(%s))').format(sql.Identifier(table)),(identities,))

    def inputs(self, requirements):
        index = IdentityIndex()
        catalogue = {}
        with self.connection() as db:
            current = db.execute("select * from lego.current_catalogue where market='US'").fetchone()
            if not current or not current['identity_import_id']:
                raise DatabaseUnavailable('The first parts catalogue is still being prepared')
            identity = current['identity_import_id']
            index.evidence = db.execute('select source from lego.identity_imports where id=%s', (identity,)).fetchone()['source']
            for row in db.execute('select * from lego.part_aliases where import_id=%s and ldraw_part=any(%s)',
                                  (identity, list({r.part for r in requirements}))):
                index.parts[row['ldraw_part']].add(row['part_num'])
            for row in db.execute("select * from lego.color_mappings where import_id=%s and system='LD' and external_id=any(%s)",
                                  (identity, list({r.color for r in requirements}))):
                index.colors[row['external_id']].add(row['color_id'])
            color_ids = list(set().union(*index.colors.values())) if index.colors else []
            for row in db.execute("select * from lego.color_mappings where import_id=%s and system='LE' and color_id=any(%s)", (identity, color_ids)):
                index.lego_colors[row['color_id']].add(row['external_id'])
            part_ids = list(set().union(*index.parts.values())) if index.parts else []
            for row in db.execute("select * from lego.part_relationships where import_id=%s and parent_part=any(%s) and kind in ('M','A')",
                                  (identity, part_ids)):
                index.relations[row['parent_part']].append((row['kind'], row['child_part']))
            part_ids += [child for values in index.relations.values() for _, child in values]
            for row in db.execute('select * from lego.element_identities where import_id=%s and part_num=any(%s) and color_id=any(%s)',
                                  (identity, part_ids, color_ids)):
                index.elements[row['part_num'], row['color_id']].add(row['element_id'])
                index.element_identity[row['element_id']] = row['part_num'], row['color_id']
            ids = list(index.element_identity)
            for row in db.execute('select payload from lego.catalogue_elements where run_id=%s and element_id=any(%s)', (current['run_id'], ids)):
                element = CatalogueElement(**row['payload']); catalogue[element.id] = element
            for row in db.execute('select payload from lego.element_observations where element_id=any(%s)', (ids,)):
                element = CatalogueElement(**row['payload'])
                if element.id not in catalogue or element.checked_at > catalogue[element.id].checked_at:
                    catalogue[element.id] = element
        return index, catalogue

    def fees(self):
        with self.connection() as db:
            row = db.execute("select r.fee_policy from lego.catalogue_runs r join lego.current_catalogue c on c.run_id=r.id where c.market='US'").fetchone()
            return row['fee_policy'] if row else []

    def observe(self, elements):
        with self.connection() as db:
            db.execute('select pg_advisory_xact_lock(3113402)')
            db.execute('create temporary table observation_batch(element_id text primary key,payload jsonb) on commit drop')
            with db.cursor().copy('copy observation_batch(element_id,payload) from stdin') as copy:
                for element in elements.values():
                    copy.write_row((element.id,json.dumps(asdict(element))))
            db.execute("""insert into lego.price_changes(element_id,old_value,new_value)
                select incoming.element_id,previous.payload,incoming.payload
                from observation_batch incoming
                join lateral (
                    select payload from (
                        select payload from lego.element_observations where element_id=incoming.element_id
                        union all select e.payload from lego.catalogue_elements e
                          join lego.current_catalogue c on c.run_id=e.run_id where e.element_id=incoming.element_id
                    ) versions order by (payload->>'checked_at')::double precision desc limit 1
                ) previous on true
                where (previous.payload->>'checked_at')::double precision < (incoming.payload->>'checked_at')::double precision
                  and (previous.payload->'unit_price',previous.payload->'availability')
                    is distinct from (incoming.payload->'unit_price',incoming.payload->'availability')""")
            db.execute("""insert into lego.element_observations(element_id,payload)
                select element_id,payload from observation_batch
                on conflict(element_id) do update set payload=excluded.payload,observed_at=now()
                where (lego.element_observations.payload->>'checked_at')::double precision
                    < (excluded.payload->>'checked_at')::double precision""")

    def save_quote(self, quote, path, source_hash, requirements):
        with self.connection() as db:
            db.execute('''insert into lego.model_revisions(id,model_path,source_sha256,requirements)
                values(%s,%s,%s,%s::jsonb) on conflict(id) do nothing''',
                       (quote['revision'], path, source_hash, json.dumps([asdict(r) for r in requirements])))
            db.execute('insert into lego.model_quotes(id,revision_id,payload) values(%s,%s,%s::jsonb)',
                       (quote['id'], quote['revision'], json.dumps(quote)))
            for line in quote['lines']:
                if line['issue']:
                    db.execute('''insert into lego.mapping_issues(revision_id,ldraw_part,ldraw_color,evidence)
                      values(%s,%s,%s,%s::jsonb) on conflict(revision_id,ldraw_part,ldraw_color)
                      do update set evidence=excluded.evidence''',
                               (quote['revision'], line['part'], line['color'], json.dumps(line)))

    def quote(self, quote_id):
        with self.connection() as db:
            row = db.execute('select payload from lego.model_quotes where id=%s', (str(uuid.UUID(quote_id)),)).fetchone()
            if not row: raise FileNotFoundError('Unknown price quote')
            return row['payload']

    def latest(self, revision):
        with self.connection() as db:
            row = db.execute('select payload from lego.model_quotes where revision_id=%s order by created_at desc limit 1', (revision,)).fetchone()
            return row['payload'] if row else None

    def bootstrap_publications(self, legacy):
        """One cutover only. Later unregistered files can never become legacy."""
        with self.connection() as db:
            db.execute('select pg_advisory_xact_lock(3113403)')
            installed = db.execute("select 1 from lego.publication_bootstraps where id='generated-v1'").fetchone()
            if not installed:
                for record in legacy:
                    db.execute('''insert into lego.model_publications(model_path,revision_id,enforced,evidence)
                        values(%s,%s,false,%s::jsonb) on conflict(model_path) do nothing''',
                        (record['file'], record['revision'], json.dumps(record)))
                db.execute("insert into lego.publication_bootstraps(id) values('generated-v1')")
            return [row['evidence'] for row in db.execute('select evidence from lego.model_publications')]

    def save_publication(self, record, quote, after):
        if quote['revision'] != record['revision'] or publication_problems(quote, after=after):
            raise ValueError('The model no longer has complete, fresh purchasing evidence')
        with self.connection() as db:
            # The quote is already immutable and persisted. Check expiry using
            # database time too; a delayed transaction must not approve stale data.
            inserted = db.execute('''insert into lego.model_publications
                (model_path,revision_id,quote_id,enforced,checked_at,expires_at,evidence)
                select %s,%s,q.id,true,to_timestamp(%s),to_timestamp(%s),%s::jsonb
                from lego.model_quotes q where q.id=%s and q.revision_id=%s
                  and q.payload=%s::jsonb and clock_timestamp() < to_timestamp(%s)
                returning model_path''',
                (record['file'],record['revision'],record['checked_at'],record['expires_at'],json.dumps(record),
                 quote['id'],record['revision'],json.dumps(quote),record['expires_at'])).fetchone()
            if not inserted:
                raise ValueError('Stock verification expired or the saved quote changed. Check the draft again.')

    def available_candidates(self):
        """Bounded keyed reads avoid a many-to-many catalogue/mapping join.

        Build reverse maps only after removing ambiguous import aliases. A
        newer observation always overrides the daily catalogue, including OOS.
        """
        from collections import defaultdict
        with self.connection() as db:
            current = db.execute("select c.*,i.source from lego.current_catalogue c join lego.identity_imports i on i.id=c.identity_import_id where c.market='US'").fetchone()
            if not current:
                raise DatabaseUnavailable('The first parts catalogue is still being prepared')
            identity = current['identity_import_id']
            catalogue = {}
            for row in db.execute("""select payload from lego.catalogue_elements where run_id=%s
                                     union all select payload from lego.element_observations""", (current['run_id'],)):
                payload = row['payload']
                if payload['id'] not in catalogue or payload['checked_at'] > catalogue[payload['id']]['checked_at']:
                    catalogue[payload['id']] = payload
            available = {key:value for key,value in catalogue.items() if value['availability']=='available'}
            aliases, ld_colors, lego_colors = defaultdict(set), defaultdict(set), defaultdict(set)
            for row in db.execute('select ldraw_part,part_num from lego.part_aliases where import_id=%s', (identity,)):
                aliases[row['ldraw_part']].add(row['part_num'])
            for row in db.execute('select system,external_id,color_id from lego.color_mappings where import_id=%s', (identity,)):
                if row['system']=='LD': ld_colors[row['external_id']].add(row['color_id'])
                else: lego_colors[row['color_id']].add(row['external_id'])
            parts, colors = defaultdict(list), defaultdict(list)
            for alias, matches in aliases.items():
                if len(matches)==1: parts[next(iter(matches))].append(alias)
            for external, matches in ld_colors.items():
                if len(matches)==1: colors[next(iter(matches))].append(external)
            result = []
            for row in db.execute('select element_id,part_num,color_id from lego.element_identities where import_id=%s and element_id=any(%s)',
                                  (identity, list(available))):
                payload = available[row['element_id']]
                if payload['color_id'] not in lego_colors[row['color_id']]: continue
                for part in parts[row['part_num']]:
                    for color in colors[row['color_id']]:
                        result.append(dict(part=part,color=color,element_id=row['element_id'],payload=payload,evidence=current['source']))
            return result

    def create_intent(self, quote_id, review_hash, available_only):
        quote = self.quote(quote_id)
        if not secrets.compare_digest(quote['review_hash'], review_hash):
            raise ValueError('The reviewed parts list changed. Review it again.')
        items = reviewed_items(quote, available_only=available_only)
        prices = {line['element_id']: line['unit_price'] for line in quote['lines'] if line['element_id']}
        items = [{**item, 'unitPrice': prices[item['elementId']]} for item in items]
        token, intent_id = secrets.token_urlsafe(32), str(uuid.uuid4())
        with self.connection() as db:
            db.execute('select pg_advisory_xact_lock(hashtext(%s))',(quote_id+review_hash,))
            existing = db.execute('select id,state from lego.cart_intents where quote_id=%s and review_hash=%s',
                                  (quote_id, review_hash)).fetchone()
            if existing:
                raise ValueError('This reviewed list already has a cart request. Resume that request instead.')
            db.execute('''insert into lego.cart_intents(id,quote_id,token_hash,review_hash,items,expires_at,state)
             values(%s,%s,%s,%s,%s::jsonb,now()+interval '10 minutes','pending')''',
                       (intent_id, quote_id, hashlib.sha256(token.encode()).hexdigest(), review_hash, json.dumps(items)))
        return {'id': intent_id, 'token': token, 'items': items, 'revision': quote['revision'],
                'expires_at': time.time() + 600, 'review_hash': review_hash}

    def claim_intent(self, intent_id, token, claim_id):
        if not claim_id or len(claim_id) > 100:
            raise ValueError('Invalid helper request ID')
        with self.connection() as db:
            row = db.execute('select *,expires_at>now() as valid from lego.cart_intents where id=%s for update',
                             (str(uuid.UUID(intent_id)),)).fetchone()
            if not row or not secrets.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
                raise ValueError('Invalid cart request')
            if row['state'] == 'pending' and row['valid']:
                db.execute("update lego.cart_intents set state='claimed',claim_id=%s where id=%s", (claim_id, intent_id))
            elif row['claim_id'] != claim_id or row['state'] not in {'claimed','partial'} or not row['valid']:
                raise ValueError('This cart request expired or was already used')
            return {'id': intent_id, 'items': row['items'], 'review_hash': row['review_hash']}

    def intent_result(self, intent_id, token, claim_id, result):
        if result.get('state') not in {'complete','partial','failed'}:
            raise ValueError('Invalid cart result')
        with self.connection() as db:
            row=db.execute('select * from lego.cart_intents where id=%s for update',(str(uuid.UUID(intent_id)),)).fetchone()
            if not row or not secrets.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest()) or row['claim_id']!=claim_id:
                raise ValueError('The cart request cannot accept this result')
            if row['state']=='complete' and row['result']==result:return {'id':intent_id,**result}
            if row['state'] not in {'claimed','partial'}:raise ValueError('The cart request cannot accept this result')
            items=result.get('items')
            if not isinstance(items,list) or len(items)!=len(row['items']):raise ValueError('Report every reviewed item')
            expected={i['elementId']:i['quantity'] for i in row['items']};seen=set()
            for item in items:
                element=item.get('elementId');requested=expected.get(element)
                if element in seen or requested is None or item.get('requested')!=requested:raise ValueError('Unexpected cart result item')
                seen.add(element)
                if any(type(item.get(k)) is not int or not 0<=item[k]<=requested for k in ['added','missing','in_bag']):
                    raise ValueError('Invalid cart result quantity')
                if item['added']+item['missing']!=requested or item['in_bag']>item['added']:raise ValueError('Inconsistent cart result quantities')
                if result['state']=='complete' and item['in_bag']!=requested:raise ValueError('The completed bag is missing pieces')
            db.execute('update lego.cart_intents set state=%s,result=%s::jsonb where id=%s',
                       (result['state'],json.dumps(result),intent_id))
        return {'id': intent_id, **result}
