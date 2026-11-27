# Development

How comfy_cloud_run works inside. For setup, see [README.md](README.md).

## Layout

| Path | Runs on | Purpose |
|---|---|---|
| `__init__.py` | local ComfyUI | Reads `.env`, registers `POST /cloud/prompt`, installs the cancel hooks |
| `cloud.py` | local ComfyUI | RunPod and R2 client; replays the cloud job's events on the local server |
| `validate.py` | local ComfyUI | Model lists for the cloud: validation against a manifest, cloud models in the GUI's dropdowns |
| `manifest.py` | local ComfyUI, `tools/` | Loading, checking and saving manifests; Dockerfile generation. No ComfyUI imports |
| `manage.py` | local ComfyUI | Routes for managing manifests from the GUI; they work without credentials |
| `release.py` | local ComfyUI | Deploy an endpoint to its `cloud/<name>` branch and create its release |
| `web/cloud_run.js` | browser | Cloud button group in the Run widget, endpoint menu, cancel button, Alt+Enter |
| `web/cloud_sidebar.js` | browser | Cloud sidebar tab |
| `worker/handler.py` | RunPod worker | Starts ComfyUI, runs one prompt per job, streams its events |
| `tools/gen_dockerfile.py` | your machine | CLI around `manifest.py`: regenerate a Dockerfile; `--pin-local` pins the local ComfyUI commit |
| `endpoints/<name>/` | – | One RunPod endpoint: `manifest.json` and the generated `Dockerfile` |

The plugin loads on the worker too, because the image copies it into `custom_nodes/`. Without `.env` it only registers a route that answers "not configured", and installs no hooks.

## Image

Built by RunPod with this repo as build context. Layer order: base → models (one layer each) → torch cu130 → ComfyUI at the pinned commit → its requirements → worker requirements → custom nodes (one layer each) → `pip` pins → plugin code. A code or dependency change never re-downloads the models. `.dockerignore` excludes `endpoints/` from the build context; only the root Dockerfile is used. `endpoints/<name>/Dockerfile` in your working copy is just a preview written on save. Whether RunPod's builder reuses cached layers between builds isn't verified.

## Job protocol

`/run` input:

```json
{"prompt_id": "<uuid>", "prompt": {"...": "API prompt"},
 "extra_data": {"extra_pnginfo": {"workflow": {}}}, "partial_execution_targets": ["9"],
 "inputs": [{"path": "sub/face.png", "url": "<presigned GET>"}, {"path": "voice.wav", "data": "<base64>"}],
 "runtime": {"models": [{"name", "directory", "url"}], "custom_nodes": [{"url", "commit"}]}}
```

The handler is an async generator, and every yield is a list of events, read through `/stream`:

- `{"type": "rejected", "error", "node_errors"}`: the worker's ComfyUI returned 400 for the prompt.
- `{"type": "accepted", "node_errors"}`
- `{"type": "<ComfyUI websocket event>", "data": {...}}`: only events for this `prompt_id`, batched for 0.5 s. `executed` events also carry `"files"`: `[{"filename", "subfolder", "type", "url"}]` uploaded to the bucket under `outputs/<prompt_id>/...`, or `[{..., "inline": "<file id>"}]` without a bucket.
- `{"type": "file_chunk", "id": "<file id>", "data": "<base64>"}`, without a bucket: one per 600 KB of an output file (about 800 KB encoded, under RunPod's 1 MB per stream item), each in its own yield, before the `executed` event that names the file.
- The stream ends after `{"type": "executing", "data": {"node": null}}`.

### Bucket optional

The bucket is optional on each side, and each side decides independently:
- **Local `.env` without `BUCKET_*`:** inputs go inline as base64. There's no local size limit, so a change of RunPod's `/run` limit (10 MB at the time of writing) needs no plugin update. If RunPod answers 413, `post_prompt` reports it with the request size and suggests a bucket; other submit errors also include the size.
- **Endpoint without `BUCKET_*`:** outputs are streamed as `file_chunk` items. The local side collects them per job (`CloudJob.chunks`) and writes the file when the `executed` event arrives.
- **Half a bucket configuration** is an error on both sides.

The handler doesn't use `return_aggregate_stream`. The client reads `/stream`, and aggregating inline files would store them a second time in the job's final output. RunPod documents no limit on a job's total streamed size; tested locally with a 4.3 MB output (6 chunks, largest stream item 0.8 MB).

Worker details:
- ComfyUI starts with the first job, not at boot: a cold worker installs the job's runtime node packs first, so it doesn't restart for them. The job's `comfy_args` (the manifest's `comfy_args`, split with `shlex`) are added to ComfyUI's command line. ComfyUI restarts when a later job brings other `comfy_args` or new node packs, or if it died. Options the handler relies on (`--listen`, `--port`, the directory options) are rejected by `check_manifest`.
- Inputs are downloaded before each job and deleted after it.
- Outputs stay on disk, because a cached output node re-sends `executed` for the existing file.
- If the generator is closed early (cancel, timeout), the handler deletes the prompt from the worker's queue and interrupts it.
- Runtime entries (`"runtime": true` in the manifest) are left out of the Dockerfile. The local backend sends the ones the prompt uses in the job's `runtime` field: models whose name is an input value, and node packs that provide one of its nodes (`validate.runtime_entries`). Before queueing, the worker:
  - downloads missing models via a `.part` file;
  - clones missing node packs into `<pack>.disabled`, which ComfyUI's loader skips, and pip-installs their requirements with torch/torchvision/torchaudio pinned to the installed versions;
  - renames each pack once installed, so a failed install never loads;
  - (re)starts ComfyUI if any pack was installed, while missing models download in parallel.

## Local backend

`POST /cloud/prompt` accepts the `/prompt` body, plus an optional `cloud_endpoint` (an endpoint folder name; the default is the first one):

1. Runs the `on_prompt` handlers and node replacements, like `/prompt`, then validates (see below). If validation fails, it answers 400 with `/prompt`'s error format, so the frontend shows red nodes and no worker starts.
2. Finds input files: string inputs that resolve via `folder_paths.get_annotated_filepath` to a file inside the input directory. Each is uploaded to `inputs/<sha256><ext>`, skipped if that key already exists.
3. Submits to RunPod `/run` and puts `(number, prompt_id, prompt, extra_data, [], {})` into `prompt_queue.currently_running` under the key `prompt_id`. The queue panel, `/queue` and `/api/jobs` then show the job.
4. A background task polls `/stream` every second. It retries on 429, 5xx and connection errors, and replays events with `send_sync(..., client_id)`. The job's `workflow_id` is merged into every event, so a concurrently running local prompt can't override it.
5. `executed`: files are downloaded into the same type and subfolder locally first. If the name is taken, it becomes `<stem>_<prompt id[:8]><ext>`. The event is rewritten to the local names.
6. At the end it calls `task_done` with the recorded status messages, then sends `executing {node: null}`, the same order as a local run. If no end event came, it records one itself: `execution_interrupted` for `CANCELLED`, otherwise `execution_error` with the RunPod error.

`rejected` becomes an `execution_error` on the first failing node, with every node error in the message. The frontend highlights that node and shows the error, as for any execution error. This only happens when the image doesn't match the manifest, e.g. before a rebuild.

### Validation

`validate_cloud_prompt` runs ComfyUI's own `execution.validate_prompt`, with model lists taken from the manifest:
- `folder_paths.get_filename_list` is replaced by a wrapper. While the `manifest_models` ContextVar is set, it returns the manifest's names for model folders (everything except `configs` and `custom_nodes`).
- The variable is only set inside the cloud request's task, so local dropdowns and local runs never see manifest models.
- Missing models therefore give the same `value_not_in_list` errors as a local run.

The same wrapper adds cloud models to the dropdowns. A middleware sets the `listed_models` ContextVar for `/object_info` requests (the only source of the frontend's combo values), and model folders then list local files plus the models of all endpoint manifests. The manifests are read on every request, so edits show after a GUI reload. Everything else still sees only local files: local `/prompt` validation, loaders, `/models/<folder>` and the model library (`/experiment/models`). A local Run with a cloud-only model therefore fails validation instead of failing to open a file.

Before that, any node whose `RELATIVE_PYTHON_MODULE` is `custom_nodes.<pack>` fails with `cloud_missing_node_pack` unless the manifest has that pack (`validate.manifest_entry`). Packs match by repository URL (`repo_key`: host/owner/repo, ignoring scheme, user, `.git` and case) or else by folder name. Registry installs often use another folder name than their repository, e.g. `deno-custom-nodes` for `comfyui-deno-custom-nodes`. The check needs custom nodes installed locally, as editing a workflow does anyway.

## Frontend

`web/cloud_run.js` calls the frontend's own `app.queuePrompt`, so widget updates (e.g. seed randomize), job tracking and error display work as for Run. The queue `number` is the only value that reaches the `/prompt` request body, so it carries a `"cloud"` marker. A wrapper around `api.fetchApi` sees the marker and sends that request to `/cloud/prompt`. Using the marker instead of a global flag keeps Run and Cloud clicked back to back apart.

The batch count is always 1: the frontend's batch setting lives in a store that extensions can't read. Frontend 1.59 tracks progress per `prompt_id`, so a local and a cloud job show their own progress side by side.

### Cloud button group

The frontend's Run widget (`[data-testid=actionbar-panel]`) has no extension slot, so `cloud_run.js` inserts its own elements after the widget's interrupt button. They then move and dock with the widget.
- A MutationObserver re-inserts them when Vue re-renders the row, and copies the Run group's current class names, so the look follows the frontend's theme and state. It only writes values that changed, so it can't trigger itself.
- The ✕ calls `POST /cloud/cancel` and is enabled while `GET /cloud/jobs` (refreshed on every `status` event) lists running jobs.
- The frontend's own ✕ also counts cloud jobs as cancellable. If clicked, the cancel hooks cancel the cloud job on RunPod.

The selected endpoint is kept in `localStorage` (`CloudRun.endpoint`) and sent as `cloud_endpoint`.

### Sidebar and management routes

| Route | Purpose |
|---|---|
| `GET /cloud/endpoints` | All endpoints with their manifests |
| `POST /cloud/endpoints` | `{"name", "copy_from"?}`: new endpoint, ComfyUI pinned to the local commit (or `v<version>` without git) |
| `PUT /cloud/endpoints/{name}` | Save a manifest (checked by `check_manifest`) and regenerate its Dockerfile |
| `POST /cloud/endpoints/{name}/missing` | `{"class_types", "models"}` from the open workflow → node packs and models the manifest lacks |
| `GET /cloud/endpoints/{name}/addable_node_packs` | Locally installed node packs with a known repository that the manifest lacks: git installs (URL and commit from `.git`) and Comfy Registry installs (URL from the `Repository` entry of their `pyproject.toml`; pinned to HEAD when added) |
| `POST /cloud/resolve_commit` | `{"url"}` → current HEAD commit via `git ls-remote`, https URLs only |

Models for "missing" come from the `models` lists that workflows embed in node properties (`{name, url, directory}`).

### Bucket

`GET /cloud/bucket` sums object sizes under `inputs/` and `outputs/`, the only prefixes the plugin and worker write. `POST /cloud/bucket/clear` deletes everything under them in batches of 1000 and refuses (409) while cloud jobs are running. Other objects in the bucket are never touched. Both need the credentials in `.env`.

### Releases

Endpoints are user configuration, so they never go into `main`: `endpoints/` is in `.gitignore`. Each endpoint has a deploy branch, `cloud/<name>`. Each commit on it is the current branch's tree plus two files: the endpoint's Dockerfile at the repo root, generated from the manifest at deploy time, and `endpoints/<name>/manifest.json` for reference. RunPod then only needs the branch, because Dockerfile path and build context stay at their defaults.

`GET /cloud/release?endpoint=<name>` builds that tree in a temporary index (`GIT_INDEX_FILE`, `read-tree HEAD`, the Dockerfile blob via `hash-object` and `update-index --cacheinfo`, `add -f` the manifest, `write-tree`), so the real index and working tree are never touched. It then compares the tree with the deployed commit, fetched into `refs/cloud-run/<name>` and reports:
- whether the Dockerfile changed since the last deploy. Manifest-only edits such as the endpoint ID don't change the image, so they don't count; the branch's manifest copy is updated with the next real deploy;
- whether HEAD is already contained in the branch;
- whether the endpoint's latest `<name>-vX.Y.Z` tag on origin points at the branch tip (via `git ls-remote`, because release tags created on GitHub aren't fetched locally);
- the next tag (patch bump), uncommitted files outside `endpoints/`, and whether `gh` is installed.

`POST /cloud/release?endpoint=<name>` (one at a time):
1. If anything changed, creates the deploy commit with `git commit-tree`. Its parents are the branch tip and HEAD, the latter only when HEAD isn't in the branch yet, so the plugin history stays connected.
2. Pushes it to origin's push URL as `refs/heads/cloud/<name>`. The local branch and HEAD don't move.

Neither step uses `refs/remotes/origin/`: a stale `origin/cloud`, e.g. from an old branch named `cloud`, would block creating `origin/cloud/<name>`.
3. Runs `gh release create <tag> --target <commit>`, or without `gh` returns GitHub's prefilled new-release URL, which the sidebar opens.

Git runs with `GIT_TERMINAL_PROMPT=0`, so missing credentials fail instead of hanging.

The status also reports `upstream_update`: an `upstream` remote exists and its tip of the current branch (via `ls-remote`) isn't in HEAD. `POST /cloud/update?endpoint=<name>` then runs `git pull --ff-only upstream <branch>` and `git push origin <branch>`, and deploys and releases as above. The Dockerfile is generated by running `tools/gen_dockerfile.py` in a subprocess, so the pulled template is used, not the one loaded in the running ComfyUI. A diverged branch fails the fast-forward and asks for a manual pull. Locally the update applies after a restart.

`POST /cloud/endpoints/restore`, called by the frontend on every page load, lists origin's `cloud/*` branches with `ls-remote` and recreates each missing endpoint from `endpoints/<name>/manifest.json` on its branch (fetched into `refs/cloud-run/<name>`, saved with `save_manifest`). Branches without that file are skipped silently.

`POST /cloud/release/restore?endpoint=<name>` fetches the latest release tag into `refs/cloud-run-release/<name>` and saves its `endpoints/<name>/manifest.json`, keeping the local `endpoint_id` and `comfy_args`: they aren't part of the image and only reach the branch with a deploy.

Open question: RunPod's docs don't say whether a release builds each endpoint's configured branch, or the commit the release points to. If it's the latter, a release for one endpoint would make the other endpoints build that endpoint's Dockerfile. Check this once with two endpoints on RunPod.

### Cancelling

A cloud job sits in `currently_running`, so core's cancel paths would otherwise match it and interrupt the *local* prompt. Two hooks prevent this:
- `prompt_queue.interrupt_if_running` is wrapped; the `/api/jobs` cancel routes use it.
- A middleware handles `POST /interrupt` with a cloud `prompt_id`, because that route checks running jobs itself.

Both call RunPod `/cancel`.

## Tests

```bash
pytest tests
```

`tests/pytest.ini` keeps pytest from importing the plugin package, which only works inside ComfyUI.

The worker can be tested without the GUI:

```bash
set -a; source .env; set +a
ENDPOINT_ID=$(python3 -c 'import json; print(json.load(open("endpoints/h3/manifest.json"))["endpoint_id"])')
curl -s https://api.runpod.ai/v2/$ENDPOINT_ID/run \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d "{\"input\": {\"prompt_id\": \"$(uuidgen | tr A-Z a-z)\", \"prompt\": $(cat workflow_api.json)}}"
curl -s https://api.runpod.ai/v2/$ENDPOINT_ID/stream/<job id> -H "Authorization: Bearer $RUNPOD_API_KEY"
```

`prompt_id` must be a lowercase UUID. `workflow_api.json` comes from **Workflow → Export (API)**.

## Not verified against the real services

- The `/stream` response shape `{"status", "stream": [{"output": ...}]}`: taken from the official `runpod` SDK, because the docs don't show it fully.
- Whether RunPod's `/cancel` closes the generator, which is what triggers the worker's interrupt.
- R2 itself: tests used moto as the S3 server.
- That Comfy Kitchen's INT8 attention kernels load in the image (`--use-ck-attention` exits at startup if they don't), and how much the ComfyUI options speed up real workflows.
