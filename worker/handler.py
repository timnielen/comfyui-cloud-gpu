"""RunPod serverless handler: runs this repo's ComfyUI and streams its websocket events.

Job input:
  {"prompt_id": "<uuid>", "prompt": {...API prompt...}, "extra_data": {"extra_pnginfo": {...}},
   "inputs": [{"path": "sub/name.png", "url": "<presigned GET>"}]}

Every yield is a list of events, read through RunPod's /stream:
  {"type": "rejected", "error": {...}, "node_errors": {...}}   the remote /prompt returned 400
  {"type": "accepted", "node_errors": {...}}
  {"type": "<ComfyUI websocket event>", "data": {...}}         events of this prompt only
"executed" events also carry "files": [{"filename", "subfolder", "type", "url"}] with presigned R2 URLs.
The stream ends after ComfyUI's final {"type": "executing", "data": {"node": null}}.

Endpoint env vars: OUT_BUCKET_ENDPOINT, OUT_BUCKET_NAME, OUT_BUCKET_KEY, OUT_BUCKET_SECRET
"""
import asyncio
import contextlib
import mimetypes
import os
import subprocess
import sys
import time
import uuid

import aiohttp
import boto3
import runpod

COMFY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
COMFY_URL = "http://127.0.0.1:8188"
DIRS = {kind: os.path.join(COMFY_DIR, kind) for kind in ("input", "output", "temp")}
BATCH_SECONDS = 0.5
URL_EXPIRY = 7 * 86400

BUCKET = os.environ["OUT_BUCKET_NAME"]
s3 = boto3.client("s3", endpoint_url=os.environ["OUT_BUCKET_ENDPOINT"], region_name="auto",
                  aws_access_key_id=os.environ["OUT_BUCKET_KEY"],
                  aws_secret_access_key=os.environ["OUT_BUCKET_SECRET"])
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with session.get(url) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk)


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


async def run_prompt(session, prompt_id, prompt, extra_data):
    client_id = uuid.uuid4().hex
    # Connect before queueing so no event of this prompt is missed.
    async with session.ws_connect(f"{COMFY_URL}/ws?clientId={client_id}", max_msg_size=0) as ws:
        async with session.post(COMFY_URL + "/prompt", json={"prompt": prompt, "prompt_id": prompt_id, "client_id": client_id, "extra_data": extra_data}) as r:
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
        inputs = [local_path("input", "", item["path"]) for item in inp.get("inputs", [])]
        try:
            await asyncio.gather(*(download(session, item["url"], path) for item, path in zip(inp.get("inputs", []), inputs)))
            async with contextlib.aclosing(run_prompt(session, inp["prompt_id"], inp["prompt"], inp.get("extra_data", {}))) as batches:
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
