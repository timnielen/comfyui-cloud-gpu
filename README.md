# comfy_cloud_run

Edit workflows in your local ComfyUI, run them on a RunPod Serverless GPU.

> **Status:** the cloud worker image is done. The local **Run on Cloud** button (progress, previews and outputs in the GUI) is in progress. Until then you can test the worker directly, see [Testing the worker](#testing-the-worker).

## How it works

- Each **endpoint** is a folder in `endpoints/<name>/` with a `manifest.json`: the ComfyUI version, custom nodes and models the cloud image contains.
- `tools/gen_dockerfile.py` turns a manifest into `endpoints/<name>/Dockerfile`. RunPod builds that image from your GitHub copy of this repo, with all models baked in.
- The worker (`worker/handler.py`) runs ComfyUI inside the image and streams its progress events back through RunPod.
- Inputs and outputs travel through your own S3-compatible bucket (e.g. Cloudflare R2). Outputs come back as presigned links.

## Setup

### 1. Install the plugin

```bash
cd ComfyUI/custom_nodes
git clone <your copy of this repo> comfy_cloud_run
```

Use your own copy: your endpoints live in this repo, and RunPod builds from it. Fork it, or push a clone to a new private repo (GitHub forks of public repos can't be private).

### 2. Bucket (Cloudflare R2)

1. Create a bucket and an API token with read/write access to it. You need its **Access Key ID** and **Secret Access Key**; the "Token value" isn't used.
2. Note the S3 endpoint **without** the bucket name: `https://<account-id>.r2.cloudflarestorage.com`, or `https://<account-id>.eu.r2.cloudflarestorage.com` for an EU bucket.
3. Recommended lifecycle rules: delete `inputs/` after 1 day and `outputs/` after 7 days.

### 3. RunPod endpoint

1. **Serverless → New Endpoint → GitHub repo**, and give the RunPod GitHub app access to your repo.
2. Set branch `main`, Dockerfile path `endpoints/h3/Dockerfile`, build context `.` (the repo root).
3. Choose a GPU with enough VRAM for your models. Allow **CUDA 13.x** hosts only (the image uses PyTorch cu130).
4. Set idle timeout to 1–5 s (a cold start is about 1 min; idle time is billed), turn FlashBoot on, and set a container disk of about 20 GB.
5. Add these endpoint environment variables:

   | Variable | Value |
   |---|---|
   | `OUT_BUCKET_ENDPOINT` | S3 endpoint from step 2 |
   | `OUT_BUCKET_NAME` | bucket name |
   | `OUT_BUCKET_KEY` | Access Key ID |
   | `OUT_BUCKET_SECRET` | Secret Access Key |

6. Builds start from a **GitHub release**, not a push. Create one, e.g. tag `v0.1.0`.
7. Copy the endpoint ID into `endpoints/h3/manifest.json` → `"endpoint_id"`, and commit it. The ID is not a secret.

### 4. Local credentials

```bash
cp .env.example .env
```

Fill in `.env`. It is gitignored and stays on your machine; the cloud worker never sees it.

| Variable | Used for |
|---|---|
| `RUNPOD_API_KEY` | Submitting, watching and cancelling jobs (RunPod → Settings → API Keys). A key restricted to your endpoints is enough. |
| `BUCKET_ENDPOINT`, `BUCKET_NAME`, `BUCKET_KEY`, `BUCKET_SECRET` | Uploading inputs and downloading outputs. Same bucket as the endpoint. |

## Day to day

Every change to the image follows the same steps: edit the manifest, run `python tools/gen_dockerfile.py <endpoint>`, commit, push, then **create a release**.

- **Add a model:** add `{"name", "directory", "url"}` to `models`. `directory` is the ComfyUI model folder (`vae`, `loras`, `diffusion_models`, …). This is the same shape as the `"models"` entries ComfyUI workflows embed in their node properties, so you can copy those.
- **Add a custom node:** add `{"url", "commit"}` to `custom_nodes`. Without `commit` you get the default branch at build time, and the build cache won't notice when it changes. Get a commit with `git ls-remote <url> HEAD`.
- **Update ComfyUI:** pull your local ComfyUI, then `python tools/gen_dockerfile.py h3 --pin-local`. This makes the cloud run the same ComfyUI commit as your local install. The commit has to exist at the manifest's `comfyui.url`, so with a patched local ComfyUI, point that url at your fork.
- **Another endpoint** (other models or GPU): copy `endpoints/h3/` to `endpoints/<name>/`, edit the manifest, generate, and create a second RunPod endpoint with Dockerfile path `endpoints/<name>/Dockerfile`.

Models are downloaded before anything else in the image, so a ComfyUI, torch or node update doesn't re-download them. Changing one model only rebuilds that model's layer, and the layers after it.

## Manifest

```json
{
  "endpoint_id": "abc123xyz",
  "comfyui": {"url": "https://github.com/Comfy-Org/ComfyUI", "commit": "<sha>"},
  "custom_nodes": [{"url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite", "commit": "<sha>"}],
  "pip": ["transformers<5"],
  "models": [{"name": "minimax_h3_audio_vae_fp32.safetensors", "directory": "vae", "url": "https://huggingface.co/..."}]
}
```

`pip` is installed after the custom nodes, so its pins win over their requirements.

## Testing the worker

Send an API-format workflow (ComfyUI: **Workflow → Export (API)**) to the endpoint:

```bash
curl -s https://api.runpod.ai/v2/$ENDPOINT_ID/run \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d "{\"input\": {\"prompt_id\": \"$(uuidgen | tr A-Z a-z)\", \"prompt\": $(cat workflow_api.json)}}"
```

Then poll `https://api.runpod.ai/v2/$ENDPOINT_ID/stream/<job id>` with the same header. `executed` events carry download links for the outputs.

Input files can be added as `"inputs": [{"path": "face.png", "url": "<presigned GET url>"}]`. Each is saved to the worker's `input/` folder under `path` and deleted after the job.

## Development

```bash
pytest tests
```
