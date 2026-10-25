# Assembly guide progress and rendering — 2026-10-25

The Copper Bean guide returned only `rendering` while it generated hundreds of
images. The renderer was active; the interface gave no progress information.
It also rendered all three camera angles for every step, although the booklet
uses only its selected view and either a secondary view or an assembly inset.

The planner now selects required images before rendering. Final home views remain
available for assembly callouts. Existing image manifests and completed PDFs stay
valid. Per-part thumbnails skip their unused CSV subprocess. Standard library
parts also omit unrelated assembly sections from the thumbnail source. Embedded
part definitions remain intact. Completed thumbnail caches remain valid. The
independent whole-model CSV comparison remains mandatory.

The status endpoint exposes bounded progress records for model checks, instruction
images, parts verification, part images and PDF layout. Cards show elapsed time and
completed-image counts. Browser storage retains the job and options by model URL
and modification time. Reloading resumes status checks. Requests have time limits,
and temporary connection failures retry without discarding the job.

## Measured checks

- Copper Bean: 447 instruction images before; 285 required images after, 36.2% fewer.
- Small Red Runabout: 48 pieces, 9 steps, 13 pages. One fresh render per version in
  the same local Docker container took 119.907 seconds before and 66.052 seconds
  after, a 44.9% reduction. Results vary by model and host load.
- Source geometry and the independent parts comparison passed.
- All 11 instruction, introduction and inventory pages matched pixel-for-pixel at
  144 DPI. The cover and completion pages differed by 262 and 282 pixels. Repeating
  the unchanged cover renderer also produced varying edge pixels; this is not a
  claim of deterministic cover rendering or exact official-reference fidelity.
- 24 toolkit tests and 4 backend guide tests passed. Tests cover selected-view
  completeness, previous-cache reuse, interrupted chunks, full-model verification,
  native/Letter reuse, progress records and rejected stale progress.
- The frontend TypeScript and production build passed.

The official-reference fidelity goal remains incomplete. This change does not
alter the printed step layouts, camera selection rules or font version.
