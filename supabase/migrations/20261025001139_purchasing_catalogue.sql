-- Private schema: deliberately absent from the Supabase Data API schema list.
create schema if not exists lego;
revoke all on schema lego from public, anon, authenticated;
grant usage on schema lego to service_role;
do $$ begin
 if not exists(select 1 from pg_roles where rolname='lego_app') then
  create role lego_app nologin;
 end if;
end $$;
grant usage on schema lego to lego_app;

create table lego.identity_imports (
 id text primary key,
 imported_at timestamptz not null default now(),
 source text not null,
 hashes jsonb not null
);
create table lego.part_aliases (
 import_id text not null references lego.identity_imports(id),
 ldraw_part text not null, part_num text not null,
 primary key (import_id, ldraw_part, part_num)
);
create table lego.color_mappings (
 import_id text not null references lego.identity_imports(id),
 system text not null check (system in ('LD','LE')),
 external_id text not null, color_id text not null,
 primary key (import_id, system, external_id, color_id)
);
create table lego.element_identities (
 import_id text not null references lego.identity_imports(id),
 element_id text not null check (element_id ~ '^[0-9]+$'),
 part_num text not null, color_id text not null,
 primary key (import_id, element_id)
);
create index element_identity_lookup on lego.element_identities(import_id, part_num, color_id);
create table lego.part_relationships (
 import_id text not null references lego.identity_imports(id),
 parent_part text not null, child_part text not null, kind text not null,
 primary key (import_id, parent_part, child_part, kind)
);
create table lego.catalogue_runs (
 id uuid primary key,
 started_at timestamptz not null default now(), finished_at timestamptz,
 market text not null default 'US' check (market='US'),
 status text not null check (status in ('running','complete','failed')),
 expected_count integer, actual_count integer, error text, fee_policy jsonb not null default '[]'
);
create table lego.catalogue_elements (
 run_id uuid not null references lego.catalogue_runs(id),
 element_id text not null, payload jsonb not null,
 primary key (run_id, element_id)
);
create table lego.current_catalogue (
 market text primary key check (market='US'),
 run_id uuid not null references lego.catalogue_runs(id),
 identity_import_id text references lego.identity_imports(id)
);
create table lego.element_observations (
 element_id text primary key,
 payload jsonb not null,
 observed_at timestamptz not null default now()
);
create table lego.price_changes (
 id bigint generated always as identity primary key,
 element_id text not null, observed_at timestamptz not null default now(),
 old_value jsonb, new_value jsonb not null
);
create index price_changes_element_time on lego.price_changes(element_id, observed_at desc);
create table lego.model_revisions (
 id text primary key check (id ~ '^[a-f0-9]{64}$'),
 model_path text not null, source_sha256 text not null,
 requirements jsonb not null, created_at timestamptz not null default now()
);
create table lego.model_quotes (
 id uuid primary key, revision_id text not null references lego.model_revisions(id),
 payload jsonb not null, created_at timestamptz not null default now()
);
create index model_quote_revision on lego.model_quotes(revision_id, created_at desc);
create table lego.mapping_issues (
 revision_id text not null references lego.model_revisions(id),
 ldraw_part text not null, ldraw_color text not null,
 evidence jsonb not null,
 primary key (revision_id, ldraw_part, ldraw_color)
);
create table lego.cart_intents (
 id uuid primary key, quote_id uuid not null references lego.model_quotes(id),
 token_hash text not null, review_hash text not null,
 items jsonb not null,
 created_at timestamptz not null default now(), expires_at timestamptz not null,
 state text not null check (state in ('pending','claimed','complete','partial','failed')),
 claim_id text, result jsonb,
 unique (quote_id, review_hash)
);

-- No public policies. Only the backend's service role may access these tables.
do $$ declare t record; begin
 for t in select tablename from pg_tables where schemaname='lego' loop
  execute format('alter table lego.%I enable row level security',t.tablename);
  execute format('revoke all on lego.%I from public, anon, authenticated',t.tablename);
  execute format('grant all on lego.%I to service_role',t.tablename);
  execute format('grant select,insert,update,delete on lego.%I to lego_app',t.tablename);
  execute format('create policy backend_only on lego.%I to lego_app using (true) with check (true)',t.tablename);
 end loop;
end $$;
grant usage, select on all sequences in schema lego to service_role;
grant usage, select on all sequences in schema lego to lego_app;
alter default privileges in schema lego revoke all on tables from public, anon, authenticated;
