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

Live cart additions remain untested and require separate approval naming a model and reviewed quantities. Automated tests use a simulated LEGO adapter, including 450 distinct pieces. The main bag must expose exact element quantities. An opaque grouped response stays unconfirmed and blocks further transfers until reviewed.

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

## Required availability for new builds

Every new model and edited version must pass the server publication check. The
builder searches only verified US Pick a Brick part/colour choices, checks draft
assemblies, and repairs unavailable choices. It may change colour, size and
construction, and must explain these adaptations. Bundled examples and previously
saved models remain available as explicit legacy revisions.

`publish_model` captures server-owned source bytes and derives quantities directly
from geometry. It rejects custom assembly surfaces, scaled parts, unsupported
composites, unresolved colours, and embedded part definitions that differ from the
installed library. It validates and renders the captured revision, refreshes every
required element, and checks identity, colour, price, status and combined order
limits again at commit. Stock and limit evidence must be at most ten minutes old.
There is no availability override. Outages and failures retain drafts and completed
renders; they do not create completed cards or approved downloads.

A private `lego.model_publications` receipt connects the exact source SHA256 to an
immutable quote. Both publication tables enable row-level security and deny browser
roles. The backend can insert/read receipts but cannot update/delete them. Local
server-only receipt copies preserve downloads during database outages. A one-time
bootstrap records existing models as legacy; later unregistered files cannot become
legacy. Generated files are owned by the server. List, model, archive, 3D export,
guide, quote and chat-artifact routes require approval of the exact model bytes.

The **All parts available** badge expires with its oldest observation. **Needs a
stock check** preserves the saved model and its historical quote. Reviewing parts
refreshes current stock; it never silently changes delivered geometry. This is a
stock observation, not a reservation or a physical construction certificate.

### Published quantity limits

LEGO's US API supplies `maxOrderQuantity` overrides, usually `null`. The current
[US Pick a Brick page](https://www.lego.com/en-us/pick-and-build/pick-a-brick)
uses a default of 999 when this override is null. `quantity_policy.py` reads the
scripts linked by that page and verifies both the exported default and its actual
Pick a Brick consumer. It does not execute downloaded JavaScript. Quotes retain
page URL, script URLs/hashes, parser version and retrieval time. Explicit lower
limits take precedence; zero stays zero. A missing API field, unknown policy,
changed bundle or stale evidence cannot approve a build. No inferred quantity
limit is used. Policy refreshes are shared for at most five minutes.

The strict geometry inventory is separate from the display BOM. LDraw library
presence alone never establishes shop availability. TypeSafe and keyword fallback
rank the same restricted candidate pool. Search never uses a part name or similar
colour to establish identity. All cart actions still require an explicit review
and user request; the builder has no cart-action tool.
