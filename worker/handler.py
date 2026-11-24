"""RunPod serverless handler: runs this repo's ComfyUI and streams its websocket events.

Job input:
  {"prompt_id": "<uuid>", "prompt": {...API prompt...}, "extra_data": {"extra_pnginfo": {...}},
   "partial_execution_targets": [...], "inputs": [{"path": "sub/name.png", "url": "<presigned GET>"}],
   "runtime": {"models": [{"name", "directory", "url"}], "custom_nodes": [{"url", "commit"}]}}
Runtime models and node packs are manifest entries left out of the image; missing ones are installed before the job.

Every yield is a list of events, read through RunPod's /stream:
  {"type": "rejected", "error": {...}, "node_errors": {...}}   the remote /prompt returned 400
  {"type": "accepted", "node_errors": {...}}
  {"type": "<ComfyUI websocket event>", "data": {...}}         events of this prompt only
"executed" events also carry "files": [{"filename", "subfolder", "type", "url"}] with presigned R2 URLs.
The stream ends after ComfyUI's final {"type": "executing", "data": {"node": null}}.

Endpoint env vars: BUCKET_ENDPOINT, BUCKET_NAME, BUCKET_KEY, BUCKET_SECRET
"""
import asyncio
import contextlib
import importlib.metadata
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import aiohttp
import boto3
import runpod

COMFY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
COMFY_URL = "http://127.0.0.1:8188"
DIRS = {kind: os.path.join(COMFY_DIR, kind) for kind in ("input", "output", "temp", "models", "custom_nodes")}
BATCH_SECONDS = 0.5
PROMPT_KEYS = ("prompt", "extra_data", "partial_execution_targets")  # job input passed on to ComfyUI's /prompt
URL_EXPIRY = 7 * 86400

BUCKET = os.environ["BUCKET_NAME"]
s3 = boto3.client("s3", endpoint_url=os.environ["BUCKET_ENDPOINT"], region_name="auto",
                  aws_access_key_id=os.environ["BUCKET_KEY"],
                  aws_secret_access_key=os.environ["BUCKET_SECRET"])
comfy = None


def start_comfy():
    global comfy
    comfy = subprocess.Popen([sys.executable, "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch"], cwd=COMFY_DIR)


async def wait_for_comfy(session):
    if comfy.poll() is not None:
        print(f"handler - ComfyUI exited with code {comfy.returncode}, restarting")
        start_comfy()
    while True:
        if comfy.poll() is not None:
            raise RuntimeError(f"ComfyUI exited with code {comfy.returncode} during startup")
        try:
            async with session.get(COMFY_URL + "/system_stats") as r:
                if r.status == 200:
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.5)


def local_path(kind, subfolder, filename):
    base = DIRS[kind]
    path = os.path.normpath(os.path.join(base, subfolder, filename))
    if os.path.commonpath((base, path)) != base:
        raise ValueError(f"{subfolder}/{filename} is outside the {kind} directory")
    return path


async def download(session, url, path):
    """Download to a .part file first, so an interrupted download never looks complete."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with session.get(url) as r:
        r.raise_for_status()
        with open(path + ".part", "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk)
    os.replace(path + ".part", path)


async def run_command(*args):
    proc = await asyncio.create_subprocess_exec(*args, cwd=COMFY_DIR)
    if await proc.wait() != 0:
        raise RuntimeError(f"{' '.join(args[:4])} failed with exit code {proc.returncode}")


async def install_node_pack(node, constraints):
    """Clone and pip-install a runtime node pack unless present. Returns whether it was installed now.

    It is installed as <name>.disabled, which ComfyUI skips, and renamed when done, so a failed install never loads.
    """
    path = local_path("custom_nodes", "", node["url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
    if os.path.isdir(path):
        return False
    print(f"handler - installing runtime node pack {os.path.basename(path)}")
    staging = path + ".disabled"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if node.get("commit"):
            await run_command("git", "init", "-q", staging)
            await run_command("git", "-C", staging, "fetch", "-q", "--depth", "1", node["url"], node["commit"])
            await run_command("git", "-C", staging, "checkout", "-q", "FETCH_HEAD")
        else:
            await run_command("git", "clone", "-q", "--depth", "1", node["url"], staging)
        requirements = os.path.join(staging, "requirements.txt")
        if os.path.isfile(requirements):
            await run_command(sys.executable, "-m", "pip", "install", "-r", requirements, "-c", constraints)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.replace(staging, path)
    return True


async def prepare_runtime(session, runtime):
    """Download missing runtime models and install missing runtime node packs, restarting ComfyUI for new packs."""
    for model in runtime.get("models", []):
        path = local_path("models", model["directory"], model["name"])
        if not os.path.exists(path):
            print(f"handler - downloading runtime model {model['directory']}/{model['name']}")
            await download(session, model["url"], path)
    if not runtime.get("custom_nodes"):
        return
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as constraints:
        # A pack's requirements must not replace the image's PyTorch build.
        pinned = [d for d in importlib.metadata.distributions() if d.metadata["Name"] in ("torch", "torchvision", "torchaudio")]
        constraints.write("".join(f"{d.metadata['Name']}=={d.version}\n" for d in pinned))
        constraints.flush()
        installed = [await install_node_pack(node, constraints.name) for node in runtime["custom_nodes"]]
    if any(installed):
        print("handler - restarting ComfyUI to load the new node packs")
        comfy.terminate()
        await asyncio.to_thread(comfy.wait)
        start_comfy()
        await wait_for_comfy(session)


def upload(prompt_id, file):
    subfolder = file.get("subfolder", "")
    path = local_path(file["type"], subfolder, file["filename"])
    key = "/".join(p for p in ("outputs", prompt_id, file["type"], subfolder, file["filename"]) if p)
    content_type = mimetypes.guess_type(file["filename"])[0] or "application/octet-stream"
    s3.upload_file(path, BUCKET, key, ExtraArgs={"ContentType": content_type})
    return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=URL_EXPIRY)


def output_files(output):
    return [f for value in (output or {}).values() if isinstance(value, list)
            for f in value if isinstance(f, dict) and "filename" in f and "type" in f]


async def prompt_events(ws, prompt_id):
    """Batches of this prompt's websocket events, flushed BATCH_SECONDS after the first event of a batch."""
    batch, deadline = [], None
    while True:
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            msg = await ws.receive(timeout=timeout)
        except asyncio.TimeoutError:
            yield batch
            batch, deadline = [], None
            continue
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise RuntimeError("ComfyUI closed the websocket")
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue  # binary preview frames
        event = msg.json()
        data = event.get("data") or {}
        if data.get("prompt_id") != prompt_id:
            continue
        if event["type"] == "executed":
            event["files"] = [{**f, "url": await asyncio.to_thread(upload, prompt_id, f)} for f in output_files(data.get("output"))]
        batch.append(event)
        if event["type"] == "executing" and data.get("node") is None:
            yield batch
            return
        if deadline is None:
            deadline = time.monotonic() + BATCH_SECONDS


async def run_prompt(session, prompt_id, body):
    client_id = uuid.uuid4().hex
    # Connect before queueing so no event of this prompt is missed.
    async with session.ws_connect(f"{COMFY_URL}/ws?clientId={client_id}", max_msg_size=0) as ws:
        async with session.post(COMFY_URL + "/prompt", json={**body, "prompt_id": prompt_id, "client_id": client_id}) as r:
            body = await r.json()
        if r.status != 200:
            yield [{"type": "rejected", "error": body.get("error"), "node_errors": body.get("node_errors", {})}]
            return
        finished = False
        try:
            yield [{"type": "accepted", "node_errors": body["node_errors"]}]
            async for batch in prompt_events(ws, prompt_id):
                yield batch
            finished = True
        finally:
            if not finished and comfy.poll() is None:  # cancelled or timed out: don't leave the prompt running for the next job
                for path, payload in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
                    async with session.post(COMFY_URL + path, json=payload):
                        pass


async def handler(job):
    inp = job["input"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=30)) as session:
        await wait_for_comfy(session)
        await prepare_runtime(session, inp.get("runtime", {}))
        inputs = [local_path("input", "", item["path"]) for item in inp.get("inputs", [])]
        try:
            await asyncio.gather(*(download(session, item["url"], path) for item, path in zip(inp.get("inputs", []), inputs)))
            async with contextlib.aclosing(run_prompt(session, inp["prompt_id"], {k: inp[k] for k in PROMPT_KEYS if k in inp})) as batches:
                async for batch in batches:
                    yield batch
        finally:
            # Every job sends all of its inputs, so none are kept for the next one.
            for path in inputs:
                if os.path.exists(path):
                    os.remove(path)


if __name__ == "__main__":
    start_comfy()
    runpod.serverless.start({"handler": handler, "return_aggregate_stream": True})
