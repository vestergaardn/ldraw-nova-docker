-- Only the server can approve a content-addressed model revision. Historical
-- approvals stay immutable; new stock checks create new quotes.
create table lego.model_publications (
 model_path text primary key check (model_path !~ '[/\\]' and model_path !~ '^\.'),
 revision_id text not null check (revision_id ~ '^[a-f0-9]{64}$'),
 quote_id uuid references lego.model_quotes(id),
 enforced boolean not null,
 checked_at timestamptz,
 expires_at timestamptz,
 published_at timestamptz not null default clock_timestamp(),
 evidence jsonb not null,
 check ((enforced and quote_id is not null and checked_at is not null
          and expires_at = checked_at + interval '10 minutes')
        or (not enforced and quote_id is null and checked_at is null and expires_at is null))
);
create index model_publication_revision on lego.model_publications(revision_id);
create table lego.publication_bootstraps (
 id text primary key check (id='generated-v1'),
 installed_at timestamptz not null default now()
);
alter table lego.model_publications enable row level security;
alter table lego.publication_bootstraps enable row level security;
revoke all on lego.model_publications, lego.publication_bootstraps from public, anon, authenticated;
grant select, insert on lego.model_publications, lego.publication_bootstraps to lego_app, service_role;
create policy server_read_publications on lego.model_publications for select to lego_app using (true);
create policy server_insert_publications on lego.model_publications for insert to lego_app with check (true);
create policy server_read_bootstrap on lego.publication_bootstraps for select to lego_app using (true);
create policy server_insert_bootstrap on lego.publication_bootstraps for insert to lego_app with check (true);
