"""Send prompts to a RunPod Serverless endpoint and replay the worker's ComfyUI events on the local server."""
import asyncio
import hashlib
import json
import logging
import os
import time
import uuid

import aiohttp
import boto3
import botocore.exceptions
from aiohttp import web

import execution
import folder_paths
from comfy_execution.jobs import validate_job_id
from server import valid_workflow_metadata, workflow_metadata_from_prompt

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ENDPOINTS_DIR = os.path.join(PLUGIN_DIR, "endpoints")
RUNPOD_API = "https://api.runpod.ai/v2"
POLL_SECONDS = 1.0
URL_EXPIRY = 86400
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
END_EVENTS = {"execution_success", "execution_error", "execution_interrupted"}
HISTORY_EVENTS = {"execution_start", "execution_cached"} | END_EVENTS


def error_response(status, message, details=""):
    """A /prompt-shaped error, so the frontend shows it like a local one."""
    error = {"type": "cloud_run_error", "message": message, "details": details, "extra_info": {}}
    return web.json_response({"error": error, "node_errors": {}}, status=status)


def endpoint_names():
    return sorted(n for n in os.listdir(ENDPOINTS_DIR) if os.path.isfile(os.path.join(ENDPOINTS_DIR, n, "manifest.json")))


def load_manifest(name):
    with open(os.path.join(ENDPOINTS_DIR, name, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def contained_path(base, *parts):
    path = os.path.normpath(os.path.join(base, *parts))
    if os.path.commonpath((base, path)) != base:
        raise ValueError(f"{os.path.join(*parts)} is outside {base}")
    return path


class CloudJob:
    def __init__(self, prompt_id, endpoint_id, job_id, client_id, workflow_metadata):
        self.prompt_id = prompt_id
        self.endpoint_id = endpoint_id
        self.job_id = job_id
        self.client_id = client_id
        self.workflow_metadata = workflow_metadata
        self.status = "FAILED"
        self.error = "Lost track of the cloud job."
        self.error_node = (None, None)
        self.messages = []
        self.outputs = {}
        self.meta = {}
        self.task = None


class CloudRun:
    def __init__(self, server, env):
        self.server = server
        self.headers = {"Authorization": f"Bearer {env['RUNPOD_API_KEY']}"}
        self.bucket = env["BUCKET_NAME"]
        self.s3 = boto3.client("s3", endpoint_url=env["BUCKET_ENDPOINT"], region_name="auto",
                               aws_access_key_id=env["BUCKET_KEY"], aws_secret_access_key=env["BUCKET_SECRET"])
        self.jobs = {}

    # Inputs

    def upload_inputs(self, prompt):
        """Upload every local input file the prompt references, keyed by content so unchanged files upload once."""
        input_dir = os.path.realpath(folder_paths.get_input_directory())
        inputs = {}
        for node in prompt.values():
            for value in node.get("inputs", {}).values():
                if not isinstance(value, str) or value in inputs:
                    continue
                path = os.path.realpath(folder_paths.get_annotated_filepath(value))
                if not os.path.isfile(path) or os.path.commonpath((input_dir, path)) != input_dir:
                    continue
                key = f"inputs/{file_digest(path)}{os.path.splitext(path)[1]}"
                try:
                    self.s3.head_object(Bucket=self.bucket, Key=key)
                except botocore.exceptions.ClientError as e:
                    if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                        raise
                    logging.info(f"Cloud Run: uploading {os.path.relpath(path, input_dir)}")
                    self.s3.upload_file(path, self.bucket, key)
                url = self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=URL_EXPIRY)
                inputs[value] = {"path": os.path.relpath(path, input_dir).replace(os.sep, "/"), "url": url}
        return list(inputs.values())

    # Outputs

    async def download(self, session, prompt_id, file):
        """Save one worker output under the same type/subfolder locally. Returns the local filename."""
        base = folder_paths.get_directory_by_type(file["type"])
        subfolder = file.get("subfolder", "")
        filename = file["filename"]
        path = contained_path(base, subfolder, filename)
        if os.path.exists(path):  # e.g. ComfyUI_00001_.png from an earlier local or cloud run
            stem, ext = os.path.splitext(filename)
            filename = f"{stem}_{prompt_id[:8]}{ext}"
            path = contained_path(base, subfolder, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        async with session.get(file["url"]) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)
        return filename

    async def local_output(self, session, job, files, output):
        """The executed output with each file renamed to where it was saved locally."""
        renamed = {}
        for file in files:
            renamed[(file["type"], file.get("subfolder", ""), file["filename"])] = await self.download(session, job.prompt_id, file)

        def local(item):
            if isinstance(item, dict) and (item.get("type"), item.get("subfolder", ""), item.get("filename")) in renamed:
                return {**item, "filename": renamed[(item["type"], item.get("subfolder", ""), item["filename"])]}
            return item
        return {k: [local(item) for item in v] if isinstance(v, list) else v for k, v in (output or {}).items()}

    # Prompt

    async def post_prompt(self, request):
        json_data = self.server.trigger_on_prompt(await request.json())
        if "prompt" not in json_data:
            return error_response(400, "No prompt provided")
        prompt = json_data["prompt"]
        try:
            prompt_id = validate_job_id(json_data["prompt_id"]) if json_data.get("prompt_id") else str(uuid.uuid4())
        except ValueError:
            return error_response(400, "prompt_id must be a valid UUID")

        names = endpoint_names()
        if not names:
            return error_response(400, "No cloud endpoints configured", f"Add one in {ENDPOINTS_DIR}")
        name = json_data.get("cloud_endpoint") or names[0]
        if name not in names:
            return error_response(400, f"Unknown cloud endpoint {name!r}", f"Available: {', '.join(names)}")
        endpoint_id = load_manifest(name)["endpoint_id"]
        if not endpoint_id:
            return error_response(400, f"Cloud endpoint {name!r} has no endpoint_id", f"Set it in endpoints/{name}/manifest.json")

        self.server.node_replace_manager.apply_replacements(prompt)
        extra_data = json_data.get("extra_data", {})
        client_id = json_data.get("client_id")
        if client_id:
            extra_data["client_id"] = client_id
        job_input = {"prompt_id": prompt_id, "prompt": prompt}
        if "extra_pnginfo" in extra_data:  # workflow embedded in the saved files
            job_input["extra_data"] = {"extra_pnginfo": extra_data["extra_pnginfo"]}
        if "partial_execution_targets" in json_data:
            job_input["partial_execution_targets"] = json_data["partial_execution_targets"]

        try:
            job_input["inputs"] = await asyncio.to_thread(self.upload_inputs, prompt)
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.post(f"{RUNPOD_API}/{endpoint_id}/run", json={"input": job_input}) as r:
                    r.raise_for_status()
                    job_id = (await r.json())["id"]
        except (aiohttp.ClientError, botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            logging.warning(f"Cloud Run: could not submit prompt: {e}")
            return error_response(502, "Could not submit the cloud job", str(e))

        logging.info(f"Cloud Run: prompt {prompt_id} is RunPod job {job_id} on endpoint {name}")
        workflow_metadata = valid_workflow_metadata(json_data) or workflow_metadata_from_prompt(extra_data) or {}
        job = CloudJob(prompt_id, endpoint_id, job_id, client_id, workflow_metadata)
        number = self.server.number
        self.server.number += 1
        extra_data["create_time"] = int(time.time() * 1000)
        queue = self.server.prompt_queue
        with queue.mutex:
            queue.currently_running[prompt_id] = (number, prompt_id, prompt, extra_data, [], {})
        self.server.queue_updated()
        self.jobs[prompt_id] = job
        job.task = asyncio.create_task(self.follow(job))
        return web.json_response({"prompt_id": prompt_id, "number": number, "node_errors": {}})

    # Following a job

    def send(self, job, event, data):
        # The job's own workflow metadata wins over whatever local prompt send_sync would merge in.
        self.server.send_sync(event, {**job.workflow_metadata, **data}, job.client_id)

    async def replay(self, session, job, event):
        kind, data = event["type"], event.get("data")
        if kind == "accepted":
            return
        if kind == "rejected":
            # Reported by finish() as an execution_error on the first failing node, which the frontend highlights.
            node_errors = event.get("node_errors") or {}
            lines = [f"{n['class_type']} #{node_id}: {e['message']}: {e['details']}" for node_id, n in node_errors.items() for e in n["errors"]]
            job.error = "The cloud worker rejected the prompt.\n" + "\n".join(lines or [(event.get("error") or {}).get("message", "")])
            if node_errors:
                node_id = next(iter(node_errors))
                job.error_node = (node_id, node_errors[node_id]["class_type"])
            return
        if kind == "executing" and data.get("node") is None:
            return  # sent by finish(), after the history entry exists, like a local run
        if kind == "executed":
            data = {**data, "output": await self.local_output(session, job, event.get("files", []), data.get("output"))}
            job.outputs[data["node"]] = data["output"]
            job.meta[data["node"]] = {"node_id": data["node"], "display_node": data.get("display_node", data["node"])}
        if kind in HISTORY_EVENTS:
            job.messages.append((kind, data))
        self.send(job, kind, data)

    async def stream(self, job):
        url = f"{RUNPOD_API}/{job.endpoint_id}/stream/{job.job_id}"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            while True:
                try:
                    async with session.get(url) as r:
                        r.raise_for_status()
                        body = await r.json()
                except aiohttp.ClientResponseError as e:
                    if e.status != 429 and e.status < 500:
                        raise
                    logging.warning(f"Cloud Run: polling job {job.job_id} failed ({e.status}), retrying")
                    await asyncio.sleep(POLL_SECONDS * 5)
                    continue
                except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                    logging.warning(f"Cloud Run: polling job {job.job_id} failed ({e!r}), retrying")
                    await asyncio.sleep(POLL_SECONDS * 5)
                    continue
                for chunk in body.get("stream", []):
                    for event in chunk["output"]:
                        await self.replay(session, job, event)
                if body["status"] in TERMINAL and not body.get("stream"):
                    break
                await asyncio.sleep(POLL_SECONDS)
            if body["status"] != "COMPLETED":
                async with session.get(f"{RUNPOD_API}/{job.endpoint_id}/status/{job.job_id}") as r:
                    status = await r.json()
                job.error = str(status.get("error") or f"Cloud job ended with status {body['status']}.")
            job.status = body["status"]

    async def follow(self, job):
        try:
            await self.stream(job)
        except Exception as e:  # the local queue entry must be finished whatever went wrong
            logging.exception(f"Cloud Run: following job {job.job_id} failed")
            job.status, job.error = "FAILED", f"Following the cloud job failed: {e}"
        finally:
            self.finish(job)

    def finish(self, job):
        ended = {kind for kind, _ in job.messages} & END_EVENTS
        if not ended and job.status == "CANCELLED":
            data = {"prompt_id": job.prompt_id, "node_id": None, "node_type": None, "executed": [], "timestamp": int(time.time() * 1000)}
            job.messages.append(("execution_interrupted", data))
            self.send(job, "execution_interrupted", data)
        elif not ended:
            data = {"prompt_id": job.prompt_id, "node_id": job.error_node[0], "node_type": job.error_node[1], "executed": [],
                    "exception_message": job.error, "exception_type": "CloudRunError", "traceback": [],
                    "current_inputs": {}, "current_outputs": [], "timestamp": int(time.time() * 1000)}
            job.messages.append(("execution_error", data))
            self.send(job, "execution_error", data)
        success = job.status == "COMPLETED" and any(kind == "execution_success" for kind, _ in job.messages)
        self.server.prompt_queue.task_done(
            job.prompt_id, {"outputs": job.outputs, "meta": job.meta},
            status=execution.PromptQueue.ExecutionStatus(status_str="success" if success else "error", completed=success, messages=job.messages),
            process_item=lambda item: item[:5])
        self.send(job, "executing", {"node": None, "prompt_id": job.prompt_id})
        del self.jobs[job.prompt_id]

    # Cancelling

    async def cancel(self, prompt_id):
        job = self.jobs[prompt_id]
        logging.info(f"Cloud Run: cancelling prompt {prompt_id} (RunPod job {job.job_id})")
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.post(f"{RUNPOD_API}/{job.endpoint_id}/cancel/{job.job_id}") as r:
                    r.raise_for_status()
        except aiohttp.ClientError as e:
            logging.warning(f"Cloud Run: cancelling RunPod job {job.job_id} failed: {e}")
