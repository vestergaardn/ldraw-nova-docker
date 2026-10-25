# Private hosted LDraw Nova

This fork adds **Create assembly guide** to every model card. It generates a
printable PDF using the model's existing construction steps. A persistent job
keeps rendering if the browser disconnects; clicking again resumes its status.
Completed PDFs are cached by the model contents and generator version.
Guide cards show the current stage, completed image count and elapsed time.
The browser remembers the guide for each model revision, so a page reload resumes
status checks. Network interruptions retry without losing the job.
Booklets render only cameras used on their pages and skip unused per-thumbnail
CSV exports. The independent full-model parts check remains mandatory.

## Hosting

- Vercel project: `vestergaard/lego-nova`. The React frontend and private gateway
  live on Vercel. **Password Protection must protect all deployments**, including
  production domains. Do not change this to Standard Protection. The owner keeps
  the access password separately from this public source repository.
- Modal app: `lego-nova`, environment `main`, workspace `inferencemaxxxing`.
  This runs the upstream Python app, ChatGPT transport, LeoCAD and LDraw library.
- Persistent volumes: `lego-nova-data` (models, chats, work and PDF jobs) and
  `lego-nova-config` (root-only AI configuration and login credentials).
- A single worker avoids concurrent writers to the upstream filesystem store.
  It starts on demand and can shut down one hour after its last request.
  Active browser streams keep requests open. Jobs without an open browser can
  be interrupted after one hour; PDF jobs show a retry message after restart.
- The private worker requires `LEGO_WORKER_KEY` on every request, including
  model downloads and viewer assets. Vercel stores this as a sensitive variable.
  It must match Modal secret `lego-nova-gateway`. Never expose it through Vite.
- Modal CPU, memory and storage charges apply. This configuration requests one
  physical CPU core and 2 GiB memory while running, with at most one container.
- Vercel limits request uploads to 4.5 MB. Local Docker supports larger uploads.

The existing app assumes a trusted single user. It is not a multi-tenant service.
New chats default to **Full access**, as requested by the owner. This allows agent
tools to run without approval clicks. Existing chats keep their selected mode.
Choose **Ask first** or **Read only** in the composer when needed.
Password Protection restricts visitors to people with the owner's password.
Vercel project administrators retain administrative access to the deployment.

## Develop and deploy

The implementation starts from upstream release `v0.6.0`. Work lives on
`assembly-guide` in both forks. Keep the upstream `AGPL-3.0` license and data
attribution files. The toolkit dependency layer is pinned in `hosting/toolkit-revision`.
Deploy from the workspace with both forks checked out side by side. Modal mounts
the current Python source separately, so code edits do not rebuild the CAD tools.

Frontend checks:

```sh
npm ci --prefix web/frontend
npm run build --prefix web/frontend
node --test hosting/tests/proxy.test.mjs
```

Run Python tests inside the Docker image, as described in
`web/backend/tests/conftest.py`. Booklet tests also live in the toolkit fork.

After changing toolkit dependencies, commit and push them first. Regenerate the cloud
Dockerfile with its immutable 40-character commit SHA:

```sh
python3 hosting/make_dockerfile.py TOOLKIT_COMMIT_SHA
uvx --python 3.12 modal deploy hosting/modal_app.py
vercel --prod --scope vestergaard
```

`Dockerfile.cloud` is generated from the upstream `Dockerfile`. The additional
source stage fetches only the pinned public toolkit revision. Runtime user
data, credentials, `.context`, `.env` and `.vercel` are excluded from builds.

The PDF generator runs as the unprivileged `agent` account with a scrubbed
environment. It validates the model, checks the independently rendered parts
count, and prints subassembly instructions before their parents. It does not
invent a physically proven assembly sequence. Models without building steps
need those steps added first. There is no additional AI call for PDF generation.
The current PDF limits are 10,000 pieces, 2,500 saved steps, a 5 MB source file,
and 3,500 seconds of rendering per attempt. Completed render chunks resume after interruption. The server runs one render at a time and queues
at most four guide jobs. A 2,103-piece model passed end-to-end rendering and geometry checks.

Choose native pages or US Letter booklet sheets in Guide options. Letter sheets
already contain two native-sized pages in fold order. Print at actual size,
both sides, short-edge flip. Do not apply booklet imposition a second time.
See `docs/purchasing-and-guides.md` for pricing, private cart requests and measured fidelity limits.

When moving a ChatGPT login between local Docker and hosting, use one installation
for generation. Two copies can compete to refresh the same sign-in session.
