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

- Backend regression run: 127 cases passed. Final budget/recovery run: 51 cases
  passed, including service failures, exhausted runtime and malformed states.
- PostgreSQL integration: 9 cases passed using isolated `lego_test` and the
  restricted application role. Identical quotes retained independent assessments;
  forged and over-budget evidence was rejected. Public roles retained no access.
- Money helpers: 3 Node tests passed for decimal parsing, exact allowance bounds
  and build-mode prompting.
- Browser fixtures cover cancel and focus restoration, Tab cycling, attachments,
  AI settings, one request per send, edit persistence, change/remove, planning,
  skip, mobile layout, changed/missing prices and immutable original assessments.
  All unexpected API requests fail the fixture, including cart requests.
- TypeScript checking and production build passed. Vite reports a bundle-size
  warning; it does not prevent the build.

Run browser fixtures against `npm run dev -- --port 5175` with
`node tests/budget-browser.mjs` from `web/frontend`. Install Playwright in the test
environment, or set `PLAYWRIGHT_MODULE` and `PLAYWRIGHT_EXECUTABLE` to an existing
installation. `BUDGET_SCREENSHOTS` optionally saves desktop/mobile screenshots.
Run money tests with `node --test tests/budget.test.mjs` on a Node release with
native TypeScript stripping. Backend tests require the existing CAD test image.

## Release

Deploy the server with `modal deploy --strategy recreate hosting/modal_app.py`
only when no chat is running. Deploy the Vercel interface after server success.
Live cart additions are excluded from these tests.

### Hosted acceptance: 2026-10-25

The initial source commit is `c8a965e78a2561cf58b82deaa8956c23b54281cf`.
The final server source is `2a945f89ef5e355f17723fb282586c95d7e13425`, adding
clean rejection of malformed budget states. Its Modal tag is `budget-2a945f8`.
Both releases reused the existing CAD image with frozen source overlays. Both
idle checks found zero running chats. Uncommitted Chrome-helper changes were
excluded from the release. The frontend remains at the initial source commit.

Vercel production deployment: `dpl_7TuJrJjNujHSxGfsbY6SihrcQj7g`, aliased to
`https://lego-nova.vercel.app`.

| Test | Pieces | Parts cost | Target | Maximum | Verified coverage |
| --- | ---: | ---: | ---: | ---: | ---: |
| Budget Test Car | 24 | $3.72 | $5.00 | $5.25 | 100% |
| Budget Test Car Edited | 24 | $3.72 | $5.00 | $5.25 | 100% |
| Unbudgeted Test Robot | 13 | $1.20 | Skipped | None | 100% |

The car compared available constructions and selected a $1.76 wheel system
instead of an option costing $3.62 for two holders alone. It added two inexpensive
front markers for a clearer front/rear distinction. The edited version changed
the body colour, retained the saved budget, and preserved the original model
bytes and original assessment exactly.

The first car turn stopped making visible progress before publication. The
controlled test explicitly stopped and continued the saved draft with its
unchanged budget. This was manual continuation, not an automatic recovery test.
Automatic restart preservation is covered by the lifecycle regression tests.

Production database evidence matched each API assessment and immutable quote.
Observation ages at publication were 7.54, 7.98 and 7.90 seconds. Quotes contained
no budget fields. All three downloads through the protected Vercel route matched
their verified content hashes. Both public database roles retained no access to
publication records. Cart-intent counts stayed at 4 before and after testing.

Unauthenticated worker and Vercel API requests returned 401. A cross-origin
request returned 403. A budget change during the running test returned 409.
The unfinished model download returned 409; private recovery records returned
404. Existing chats retained skipped budgets and existing models had no new
budget claims.

Connected Chrome showed the budget prompt before creation, $5.00 → $5.25,
disabled budget controls during generation, and both completed $3.72 assessments.
Changing the idle chat target to $4.00 left both historical $5.00 records intact.
Removing the budget showed an unlimited chat; restoring it saved $5.00 again.
Service fees appeared separately from the parts subtotal.
After the final idle deployment, the $5.00 chat budget and both original
assessments survived the restart. Malformed states and fractional-cent amounts
returned 400. Private access and all three verified downloads still passed.

Private reproducible evidence and screenshots are in the workspace's
`.context/budget-hosted/` and `.context/budget-ui/` directories.
