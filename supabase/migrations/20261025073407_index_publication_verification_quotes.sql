-- Index the verification-quote foreign key for immutable publication history.
create index model_publication_quote on lego.model_publications(quote_id);
