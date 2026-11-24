# comfy_cloud_run

Edit workflows in your local ComfyUI, run them on a RunPod Serverless GPU. Progress and results show up in your local GUI, and you don't need the models locally.

You need a [RunPod](https://www.runpod.io) account, an S3-compatible bucket such as [Cloudflare R2](https://developers.cloudflare.com/r2/), and a GitHub account.

## Setup

### 1. Install the plugin

RunPod builds your images from a GitHub repository you own, and the plugin pushes your endpoints there. So the plugin needs its own copy:

1. On GitHub, create an empty repository, e.g. `comfy_cloud_run`. Private is fine; don't add a README.
2. Install the plugin and make your repository its `origin`:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/timnielen/comfy_cloud_run.git
cd comfy_cloud_run
git remote rename origin upstream
git remote add origin git@github.com:<your-user>/comfy_cloud_run.git
git push -u origin main
pip install -r requirements.txt
```

To get plugin updates later: `git pull upstream main`, then `git push`. A public fork works too: clone your fork instead and skip the remote commands.

### 2. Credentials

In the plugin folder, copy `.env.example` to `.env`, then:

1. In RunPod, create an API key with read/write access (Credentials → Create API Key → Serverless endpoints → tick read and write → Create).
2. Create a bucket, e.g. in Cloudflare R2, and an API token with read/write access to it (R2: "Object Read & Write").
3. Add lifecycle rules to the bucket that delete `inputs/` after 1 day and `outputs/` after 7 days.

Fill in `.env`:

| Line | Value |
|---|---|
| `RUNPOD_API_KEY` | the RunPod API key |
| `BUCKET_NAME` | the bucket's name |
| `BUCKET_ENDPOINT` | the bucket's S3 API endpoint **without** the bucket name, e.g. `https://<account-id>.r2.cloudflarestorage.com` (`<account-id>.eu.r2…` for an EU bucket) |
| `BUCKET_KEY` | the token's Access Key ID |
| `BUCKET_SECRET` | the token's Secret Access Key |

R2 also shows a "Token value" for the token; it isn't needed. `.env` is not committed and stays on your computer.

### 3. First endpoint

1. Start ComfyUI, open the **Cloud** tab in the sidebar and create an endpoint with **New…**, e.g. `flux`. Add its models and node packs.
2. Press **Deploy & release**. This creates the branch `cloud/<name>` in your repo, with the endpoint's Dockerfile, and its first release.

### 4. RunPod endpoint

1. **Serverless → New Endpoint → GitHub repo**, and select your copy of this repo.
2. Branch **`cloud/<name>`**, e.g. `cloud/flux`. Leave the Dockerfile path and build context at their defaults.
3. Choose a GPU with enough VRAM for your models
4. Idle timeout 1–5 s, execution timeout 3600 s (1 hour), FlashBoot on.
5. Environment Variables: Add the environment variables `BUCKET_ENDPOINT`, `BUCKET_NAME`, `BUCKET_KEY` and `BUCKET_SECRET`: the same lines as in your `.env`. The worker doesn't need `RUNPOD_API_KEY`, so leave that one out.
6. Container Configuration: Container disk 20 GB
7. Advanced: allow **CUDA 13.x** only.
8. Paste the endpoint's ID into the Cloud tab. If no build starts, press **Deploy & release** again.

## Running

Press **☁ Cloud · endpoint** (showing the selected endpoint) next to the Run button, or **Alt+Enter**. Its arrow picks the endpoint, and its ✕ cancels running cloud jobs. The run shows up in the queue like a local one, and outputs are saved to your local `output/` folder. You can run locally and in the cloud at the same time.

Model dropdowns also list the models of your cloud endpoints, so you can pick them without having them locally. A local Run with such a model reports it as missing.

The **Cloud** tab's **Bucket** section shows how much of R2's free 10 GB the cloud inputs and outputs use. **Clear bucket** deletes them. Outputs already saved to your `output/` folder are kept.

Before anything is sent, the workflow is checked against the endpoint's manifest. Models or node packs the cloud image doesn't have are marked on the nodes, as they would be for a local run, and no worker is started.

## Changing what's in the cloud

Open the **Cloud** tab in the sidebar, or **Manage endpoints…** in the Cloud button's menu. There you can:
- add and remove models and node packs;
- add what the open workflow needs but the endpoint lacks;
- set the RunPod endpoint ID;
- create new endpoints, e.g. one for Flux and one for MiniMax H3, each with its own models and GPU. For a new endpoint, deploy it, then create a RunPod endpoint for its branch, as in setup step 4.

Changes are saved to the endpoint's folder in this repo, `endpoints/<name>/`. To update an endpoint's image, select it and press **Deploy & release** at the bottom of the tab:
1. It commits the endpoint's Dockerfile and manifest, together with the plugin code of your current branch, to the branch `cloud/<name>`, and pushes that branch.
2. It creates the endpoint's next GitHub release, e.g. `flux-v0.1.3`, which makes RunPod rebuild.

Your `main` branch is never changed. `endpoints/` is ignored there, so your setup stays out of the plugin code. Uncommitted code changes aren't included.

On another computer, get an endpoint back from its branch: `git fetch && git restore --source origin/cloud/<name> --worktree -- endpoints/<name>`.

With the [GitHub CLI](https://cli.github.com) installed and logged in (`gh auth login`), the release is created directly. Without it, GitHub's release page opens with everything filled in, and you click **Publish release**.

### Editing manifests by hand

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

After editing it, press **Deploy & release** in the Cloud tab. To look at the Dockerfile first, run `python tools/gen_dockerfile.py <name>`; it writes `endpoints/<name>/Dockerfile`.

- **Models:** `directory` is the model folder, e.g. `vae`, `loras` or `diffusion_models`. Workflows often list their models in exactly this form, so you can copy them.
- **Custom nodes:** pin a `commit` (`git ls-remote <url> HEAD`), so you always get the same version.
- **ComfyUI version:** `python tools/gen_dockerfile.py <name> --pin-local` makes the cloud use the same ComfyUI version as your local install.

Developers: see [DEVELOPMENT.md](DEVELOPMENT.md).
