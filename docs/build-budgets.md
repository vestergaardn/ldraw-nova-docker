# Optional parts budgets

New chats ask for a USD parts budget after Send and before the first writable
build. Planning does not ask. Cancel preserves the draft, attachments and AI
settings. Skip explicitly disables the spending limit. Existing chats default to
skipped. Budgets carry across revisions and can change only while idle.

`BuildBudget` has `pending`, `skipped` and `set` states. Amounts are positive
integer cents. The maximum is `floor(amount_cents * 105 / 100)`. The fixed 5%
allowance does not include service fees, shipping or tax. Each user request and
server recovery record captures the selected rule. Later chat settings cannot
change a running request's rule.

The building AI receives the target, maximum and design priorities. Available
part search accepts `max_unit_price` in cents; filtering precedes TypeSafe or
keyword ranking. Draft checks return the complete parts subtotal, remaining
budget, excess and largest costs. Publication checks run before expensive
rendering and again after fresh post-render observations. The commit boundary
and database evidence writer independently validate the assessment. Availability,
exact identity, colours, quantity limits and the ten-minute evidence limit remain
mandatory, including for skipped budgets.

`BudgetAssessment` is stored in the existing private `model_publications.evidence`
JSON column. No migration or new public access is needed. Price quotes contain no
budget: identical model bytes may have different request budgets without sharing
a spending rule. Published assessments remain immutable. The interface displays
the original result and separately compares the latest quote with that original
budget. Missing prices are unknown, never zero. Delivered geometry never changes
when prices change.

The design objective is qualitative, not proof of a globally optimal model.
The AI should preserve the concept and defining details, compare available
colours and construction, and avoid filler pieces. Over-budget drafts cannot
publish. Exhausted work limits retain drafts and explain continuation options.
Cart requests remain explicit; budget controls never call cart endpoints.

## Verification

- Backend regression run: 127 cases passed. Final budget/recovery run: 46 cases
  passed, including two additional service-failure and exhausted-runtime cases.
- PostgreSQL integration: 9 cases passed using isolated `lego_test` and the
  restricted application role. Identical quotes retained independent assessments;
  forged and over-budget evidence was rejected. Public roles retained no access.
- Money helpers: 3 Node tests passed for decimal parsing, exact allowance bounds
  and build-mode prompting.
- Browser fixtures cover cancel and focus restoration, Tab cycling, attachments,
  AI settings, one request per send, edit persistence, change/remove, planning,
  skip, mobile layout, changed/missing prices and immutable original assessments.
  All unexpected API requests fail the fixture, including cart requests.
- TypeScript checking and production build passed. Vite reports the existing
  large-bundle warning; it does not prevent the build.

Run browser fixtures against `npm run dev -- --port 5175` with
`node tests/budget-browser.mjs` from `web/frontend`. Install Playwright in the test
environment, or set `PLAYWRIGHT_MODULE` and `PLAYWRIGHT_EXECUTABLE` to an existing
installation. `BUDGET_SCREENSHOTS` optionally saves desktop/mobile screenshots.
Run money tests with `node --test tests/budget.test.mjs` on a Node release with
native TypeScript stripping. Backend tests require the existing CAD test image.

## Release

Deploy the server with `modal deploy --strategy recreate hosting/modal_app.py`
only when no chat is running. Deploy the Vercel interface after server success.
Hosted generation, pricing, downloads and private access checks are recorded
below after deployment. Live cart additions are excluded from these tests.
