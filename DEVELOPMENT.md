# Development

How comfy_cloud_run works inside. For setup, see [README.md](README.md).

## Layout

| Path | Runs on | Purpose |
|---|---|---|
| `__init__.py` | local ComfyUI | Reads `.env`, registers `POST /cloud/prompt`, installs the cancel hooks |
| `cloud.py` | local ComfyUI | RunPod and R2 client; replays the cloud job's events on the local server |
| `validate.py` | local ComfyUI | Model lists for the cloud: validation against a manifest, cloud models in the GUI's dropdowns |
| `web/cloud_run.js` | browser | Run on Cloud button, Alt+Enter command |
| `worker/handler.py` | RunPod worker | Starts ComfyUI, runs one prompt per job, streams its events |
| `tools/gen_dockerfile.py` | your machine | `manifest.json` → `Dockerfile`; `--pin-local` pins the local ComfyUI commit |
| `endpoints/<name>/` | – | One RunPod endpoint: `manifest.json` and the generated `Dockerfile` |

The plugin loads on the worker too, because the image copies it into `custom_nodes/`. Without `.env` it only registers a route that answers "not configured", and installs no hooks.

## Image

Built by RunPod with this repo as build context. Layer order: base → models (one layer each) → torch cu130 → ComfyUI at the pinned commit → its requirements → worker requirements → custom nodes (one layer each) → `pip` pins → plugin code. A code or dependency change never re-downloads the models. Whether RunPod's builder reuses cached layers between builds isn't verified.

## Job protocol

`/run` input:

```json
{"prompt_id": "<uuid>", "prompt": {"...": "API prompt"},
 "extra_data": {"extra_pnginfo": {"workflow": {}}}, "partial_execution_targets": ["9"],
 "inputs": [{"path": "sub/face.png", "url": "<presigned GET>"}]}
```

The handler is an async generator, and every yield is a list of events, read through `/stream`:

- `{"type": "rejected", "error", "node_errors"}`: the worker's ComfyUI returned 400 for the prompt.
- `{"type": "accepted", "node_errors"}`
- `{"type": "<ComfyUI websocket event>", "data": {...}}`: only events for this `prompt_id`, batched for 0.5 s. `executed` events also carry `"files": [{"filename", "subfolder", "type", "url"}]`, uploaded to R2 under `outputs/<prompt_id>/...`.
- The stream ends after `{"type": "executing", "data": {"node": null}}`.

Worker details:
- ComfyUI starts once per worker and restarts if it died.
- Inputs are downloaded before each job and deleted after it.
- Outputs stay on disk, because a cached output node re-sends `executed` for the existing file.
- If the generator is closed early (cancel, timeout), the handler deletes the prompt from the worker's queue and interrupts it.

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

Before that, any node whose `RELATIVE_PYTHON_MODULE` is `custom_nodes.<pack>` fails with `cloud_missing_node_pack` if `<pack>` doesn't match a manifest repo name (case-insensitive). The check needs custom nodes installed locally, as editing a workflow does anyway.

## Frontend

`web/cloud_run.js` calls the frontend's own `app.queuePrompt`, so widget updates (e.g. seed randomize), job tracking and error display work as for Run. The queue `number` is the only value that reaches the `/prompt` request body, so it carries a `"cloud"` marker. A wrapper around `api.fetchApi` sees the marker and sends that request to `/cloud/prompt`. Using the marker instead of a global flag keeps a local Run and Run on Cloud clicked back to back apart.

The batch count is always 1: the frontend's batch setting lives in a store that extensions can't read. Frontend 1.59 tracks progress per `prompt_id`, so a local and a cloud job show their own progress side by side.

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
