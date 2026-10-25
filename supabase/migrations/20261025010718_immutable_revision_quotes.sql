-- Historical revision requirements and quoted values are append-only for the app.
-- A fresh calculation creates a new quote; it never revises a historical amount.
revoke update, delete on lego.model_revisions, lego.model_quotes from lego_app;
