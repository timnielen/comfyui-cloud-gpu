# comfy_cloud_run

Edit workflows in your local ComfyUI, run them on a RunPod Serverless GPU. Progress and results show up in your local GUI, and you don't need the models locally.

You need a [RunPod](https://www.runpod.io) account, an S3-compatible bucket such as [Cloudflare R2](https://developers.cloudflare.com/r2/), and a GitHub account.

## Setup

### 1. Install the plugin

Make your own copy of this repo: your cloud setup lives in it, and RunPod builds from it. Fork it, or push a clone to a new private repo. Then:

```bash
cd ComfyUI/custom_nodes
git clone <your copy of this repo> comfy_cloud_run
pip install -r comfy_cloud_run/requirements.txt
```

### 2. Bucket

1. Create a bucket and an API token with read/write access to it.
2. Note the **Access Key ID**, the **Secret Access Key** and the S3 endpoint **without** the bucket name, e.g. `https://<account-id>.r2.cloudflarestorage.com`.
3. Add lifecycle rules that delete `inputs/` after 1 day and `outputs/` after 7 days.

### 3. RunPod endpoint

1. **Serverless → New Endpoint → GitHub repo**, and select your copy of this repo.
2. Branch `main`, Dockerfile path `endpoints/h3/Dockerfile`, build context `.`.
3. Choose a GPU with enough VRAM for your models, and allow **CUDA 13.x** only.
4. Idle timeout 1–5 s, FlashBoot on, container disk about 20 GB.
5. Add the environment variables `OUT_BUCKET_ENDPOINT`, `OUT_BUCKET_NAME`, `OUT_BUCKET_KEY` and `OUT_BUCKET_SECRET` with your bucket's values.
6. Create a **GitHub release** (e.g. `v0.1.0`). This starts the build.
7. Put the endpoint ID into `endpoints/h3/manifest.json` → `"endpoint_id"` and commit it.

### 4. Credentials

In the plugin folder, copy `.env.example` to `.env` and fill it in:

- `RUNPOD_API_KEY`: from RunPod → Settings → API Keys.
- `BUCKET_*`: the same bucket values as in step 3.

`.env` is not committed, and it stays on your computer.

## Running

Press **Run on Cloud** next to the Run button, or **Alt+Enter**. The run shows up in the queue like a local one, and outputs are saved to your local `output/` folder. You can run locally and in the cloud at the same time.

Before anything is sent, the workflow is checked against the endpoint's manifest. Models or node packs the cloud image doesn't have are marked on the nodes, as they would be for a local run, and no worker is started.

## Changing what's in the cloud

Each endpoint has a manifest, `endpoints/<name>/manifest.json`, that lists what its image contains:

```json
{
  "endpoint_id": "abc123xyz",
  "comfyui": {"url": "https://github.com/Comfy-Org/ComfyUI", "commit": "<sha>"},
  "custom_nodes": [{"url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite", "commit": "<sha>"}],
  "pip": ["transformers<5"],
  "models": [{"name": "minimax_h3_audio_vae_fp32.safetensors", "directory": "vae", "url": "https://huggingface.co/..."}]
}
```

After editing it:

```bash
python tools/gen_dockerfile.py h3
```

Then commit, push and create a new GitHub release.

- **Models:** `directory` is the model folder, e.g. `vae`, `loras` or `diffusion_models`. Workflows often list their models in exactly this form, so you can copy them.
- **Custom nodes:** pin a `commit` (`git ls-remote <url> HEAD`), so you always get the same version.
- **ComfyUI version:** `python tools/gen_dockerfile.py h3 --pin-local` makes the cloud use the same ComfyUI version as your local install.
- **More endpoints**, e.g. for other models or GPUs: copy `endpoints/h3/` to a new folder and create a second RunPod endpoint for its Dockerfile.

Developers: see [DEVELOPMENT.md](DEVELOPMENT.md).
