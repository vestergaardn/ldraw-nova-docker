# Restart recovery: 2026-10-25

## Failure and correction

The harbour-crane chat was cancelled during replacement of the Modal worker.
Application shutdown called the same cancellation path as the user's Stop
button. The transcript survived, but the active turn existed only in memory.
Startup had no record of work to resume.

Each new turn now has a server-owned `run.json` beside its transcript. It stores
the turn ID, original AI entry, options, progress budget, recovery count and
terminal/interrupted status. The initial request and state are committed before
work starts. Completed tool exchanges are committed before moving on.

Shutdown marks unfinished turns interrupted; startup resumes them from their
saved history and work files. User stops, completed turns, provider errors and
exhausted budgets remain stopped. Recovery does not invent another user message
or automatically replay unfinished commands. The AI receives an interrupted
tool result and instructions to inspect existing outputs before continuing.
One-time tool approvals must be requested again. Stock/publication checks remain
mandatory. A final answer already on disk is reconciled without another AI call.

The limit is three automatic recoveries per request. Existing chats without run
records need an explicit continuation message. The private run record is not
downloadable through the file API.

## Hosting boundary

This remains a single-writer filesystem application. Keep `max_containers=1`
and deploy with `--strategy recreate`. The replacement reloads the volumes before
recovering turns. Startup after an idle shutdown happens on the next request.
Running work resumes from the last saved checkpoint, not the exact interrupted
instruction. Interrupted commands may need inspection or repair.

The deployed source commit is `aa682bf4ff857329fc78df9e6a142cd37f81d009`.
The isolated release used the existing CAD image with that commit's source
overlays. Uncommitted Chrome-helper work was excluded. No Vercel code changed.

## Verification

- The initial lifecycle regression failed: `The server forgot the interrupted build`.
- 101 relevant backend tests passed, including 18 recovery cases.
- Tests cover shutdown/startup, abrupt process exit without cleanup, explicit
  Stop, Stop during initial persistence, duplicate recovery calls, saved tool
  results, fresh approvals, original permissions, budgets, repeated restarts,
  missing models, failed storage, completed-answer reconciliation and private
  run records.
- The same suite included publication, purchasing API, model-builder settings,
  document and existing agent/chat/store tests.
- A live test ran a command with a 120-second sleep. After its start marker
  reached the persistent volume, the only active worker was restarted with
  `modal app rollover lego-nova --strategy recreate`.
- The replacement resumed the same turn exactly once. It preserved the original
  options and one user message, completed the test without repeating the sleep,
  and persisted `status=completed`, `recoveries=1`. No model was created.
- Unauthenticated worker and Vercel API requests both returned 401.

Private live evidence is in the workspace's `.context/recovery-live/` directory.
The original harbour-crane chat was then explicitly continued with its original
AI model and settings. No further server restart is needed for that build.
