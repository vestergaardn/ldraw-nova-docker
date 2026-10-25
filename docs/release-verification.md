# Release verification — 2026-10-25 UTC

## Released behavior

The private app is https://lego-nova.vercel.app. Vercel serves the interface; the private Modal gateway serves model, guide and purchasing routes. The dedicated Supabase project is `rhagjalxvpugqgynzdql`.

- Daily US catalogue refresh completed with 17,975 elements. The current coverage report verifies 17,697 identities and colours, with five missing identities and 273 colour mismatches. The daily schedule is 06:00 UTC.
- Revision quotes remain immutable. Price timestamps show the oldest relevant observation, separately from quote creation. Cards poll until background price updates finish. Missing prices never produce a complete zero-price quote.
- The 48-piece car has 37 purchasable pieces, $5.87 in known prices, four unresolved rows, and no complete subtotal. The published $7 service fee below $14 remains conditional. Shipping and tax stay with LEGO checkout.
- The hosted car guide has 13 native pages, nine steps and eight US Letter sides. Both downloads returned HTTP 200. Source geometry and the independent rendered parts count passed.
- A separate 2,103-piece fixture produced 95 native pages and 48 Letter sides. The source and parts checks passed.
- Outfit Regular, Medium and Bold have distinct embedded faces. Font instancing updates PostScript names to prevent ReportLab from collapsing weights. The PDF cache versions font changes separately from reusable geometry images.

## Validation

| Checks | Passed |
| --- | ---: |
| Toolkit booklet, plan, font embedding and interrupted-render tests | 10 |
| Purchasing domain, provider and isolated PostgreSQL integration tests | 15 |
| Chrome helper simulated cart and message-boundary tests | 14 |
| Private Vercel gateway tests | 3 |
| Backend guide, API, sandbox, toolkit, model deletion and purchasing tests | 45 |

The frontend TypeScript build passed. Desktop and 390-pixel-wide mobile checks passed, with horizontal scrolling confined to the parts table. Production, deployment and worker URLs rejected unauthenticated requests with HTTP 401. Anonymous and authenticated Supabase Data API roles have no access to the private schema.

An earlier deployment left a pricing transaction open and blocked a shared lock. The isolated timeout test reproduced the missing protection. Connections now explicitly enforce a 30-second statement limit, a 30-second idle transaction limit and a five-second lock limit. Observation writes use one batch. Only the confirmed abandoned pricing sessions were closed. The original live price request then completed in 5.62 seconds.

## Incomplete work and constraints

**Exact reference fidelity remains incomplete.** Four strict page comparisons fail at 72 and 144 DPI. At 144 DPI, reference page 3 differs in 64,727 pixels and page 34 differs in 101,828 pixels. Layout and geometry counts remain separate in the toolkit report. No tolerance or pasted-page image converts this into a pass. The other page archetypes have reusable components but lack exact-reference proof.

Outfit won the corrected five-font, forty-sample comparison: mean ink overlap 0.67750 and mean absolute visible-width error 3.063%. These are glyph measurements, not whole-page similarity. Cera Pro was not purchased or extracted.

The Chrome helper is version 0.1.1, with a version-3 mutation journal. It requires exact main-bag quantity readback. Opaque grouped responses stop as unconfirmed; they never trigger a repeated transfer. Tests use a simulated LEGO adapter, including 450 distinct pieces. No live cart additions or checkout were performed. A live test still requires a separate request naming a model and reviewed quantities.

Installation files are prepared at `~/Library/Application Support/LEGO Nova/chrome-helper`. Browser security policy blocks automated access to `chrome://extensions` and forbids alternate automation. Manual loading is required unless the user has completed it.

Physical construction certification is outside this release.
