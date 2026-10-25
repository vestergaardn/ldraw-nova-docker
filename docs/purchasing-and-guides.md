# Private instructions and parts ordering

The Vercel app stays behind password protection on every deployment. The Modal worker requires a private gateway key on every route. Supabase uses the private `lego` schema and a restricted `lego_app` login. Anonymous and authenticated Data API roles have no schema access. Model revisions and price quotes are append-only for the application.

## Pricing

Every quote derives its parts list from a captured model source in a private server folder. Editable model CSV files cannot change purchase quantities. Rebrickable bulk import-direction aliases, explicit colour mappings and element identities establish purchase matches. Names and RGB similarity do not establish a match. A composite that has its own verified element stays a composite.

The US shop snapshot currently contains 17,975 element IDs. Of those, 17,697 have matching Rebrickable identities and LEGO colour evidence; five lack an identity and 273 have unresolved colour differences. `catalogue-coverage.json` records the exact snapshot and definitions. These are catalogue counts, not a guarantee that a generated model is orderable.

The 48-piece car has 37 purchasable pieces and $5.87 of known part prices. Eleven pieces remain unresolved or unlisted across four parts-list rows. Missing prices remain null. The published $7 service fee below $14 is conditional; LEGO determines the actual fee, shipping and tax at checkout.

`refresh_catalogue` runs daily at 06:00 UTC on Modal. It fetches Rebrickable bulk files at most once per UTC day, uses bounded shop requests and backoff, validates every page, and atomically promotes only complete snapshots. A failed run preserves the previous complete catalogue. Two full catalogue and identity snapshots are retained; historical quotes, mapping evidence and price changes remain. Local bulk downloads retain seven days.

The app recalculates each model revision, then refreshes its required element IDs in batches of at most 100. Quote JSON, database credentials and purchasing intents stay outside the agent sandbox. The browser receives reviewed item data, never database or LEGO session credentials.

## Cart helper

Download the private extension from the parts review. `chrome-helper/README.md` contains installation and recovery instructions. Site origins and top-level frames are restricted. No model-generation tool can create a cart intent. Only an explicit Add to LEGO bag click creates a short-lived review-bound request.

A request is single-use, but its original helper claim may resume while valid. Chrome stores a journal before mutations. It reconciles lost add responses, preserves existing quantities, splits large lists into compatible batches and verifies the bag after transferring each group. It never retries an ambiguous transfer, substitutes a part, pays or checks out.

Live cart additions remain untested and require separate approval naming a model and reviewed quantities. Automated tests use a simulated LEGO adapter, including 450 distinct pieces.

## Instruction engine

`InstructionPlan` records steps, cameras, repeated assemblies, arrows, page positions, original source hash and a geometry-preservation hash. Improvements insert STEP boundaries while preserving all other records and their order. Source checks and an independent LeoCAD parts-list comparison must pass. This does not certify physical construction.

The Creator template uses measured 31134 page dimensions and colours. The City clear option reuses those components with white paper; it is not a separate exact City reference. The selected free font is Outfit under SIL OFL 1.1. It won a five-family, forty-sample raster comparison. See the toolkit's `docs/instructions/fonts.md`.

Native PDFs use 382.677 × 289.134 points. US Letter PDFs use booklet imposition: native pages arranged on 792 × 612-point sheets. Print at actual size, duplex, short-edge flip. Fold through the sheet centre. Completed render chunks persist and are hash-checked before reuse after interruption.

**Exact reference fidelity is incomplete.** Fixed pages 3 and 34 are regenerated from LDraw geometry, then compared with the official PDF using the same Poppler binary at 72 and 144 DPI. The strict zero-difference tests fail. Layout and geometry differences are recorded separately in `ldraw-nova/docs/instructions/reference-fidelity.json`. No pasted reference-page image can satisfy these tests.

## Operations

- Migration history: `20261025001139_purchasing_catalogue`, `20261025010718_immutable_revision_quotes`.
- Modal secret: `lego-nova-purchasing`, key `LEGO_DATABASE_URL` (restricted pooler login).
- Scheduled worker: `lego-nova.refresh_catalogue`; catalogue files live in `lego-nova-catalogue`.
- App data and configuration remain in the existing independent Modal volumes.
- Database project: `rhagjalxvpugqgynzdql`, Free plan, US East.
- No font licence was purchased. No live cart was modified.
