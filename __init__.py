"""Cloud Run: run prompts from the local GUI on a RunPod Serverless endpoint. The cloud worker itself lives in worker/."""
import asyncio
import json
import logging
import os
import sys

from aiohttp import web

from server import PromptServer
from .cloud import CloudRun, error_response
from . import manage, manifest, release
from .manifest import PLUGIN_DIR, endpoint_names, load_manifest
from .validate import listed_models, model_lists

BUCKET_KEYS = ("BUCKET_ENDPOINT", "BUCKET_NAME", "BUCKET_KEY", "BUCKET_SECRET")  # optional, but all or none
ENV_KEYS = ("RUNPOD_API_KEY", *BUCKET_KEYS)
ENV_FILE = os.path.join(PLUGIN_DIR, ".env")
NODE_CLASS_MAPPINGS = {}
WEB_DIRECTORY = "./web"


def load_env():
    """KEY=VALUE lines from .env; process environment variables take precedence."""
    env = {}
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    env[key.strip()] = value.strip().strip("'\"")
    env.update({k: os.environ[k] for k in ENV_KEYS if k in os.environ})
    return env


server = PromptServer.instance
env = load_env()
missing = [] if env.get("RUNPOD_API_KEY") else ["RUNPOD_API_KEY"]
if any(env.get(k) for k in BUCKET_KEYS):  # half a bucket configuration is a mistake, not "no bucket"
    missing += [k for k in BUCKET_KEYS if not env.get(k)]
cloud = None if missing else CloudRun(server, env)

if cloud is not None:
    # Cloud jobs sit in the local running queue, so the core cancel paths must not interrupt the local prompt for them.
    interrupt_local = server.prompt_queue.interrupt_if_running

    def interrupt_if_running(prompt_id):
        if prompt_id in cloud.jobs:
            asyncio.ensure_future(cloud.cancel(prompt_id))
            return True
        return interrupt_local(prompt_id)

    server.prompt_queue.interrupt_if_running = interrupt_if_running

    @web.middleware
    async def cloud_interrupt(request, handler):
        if request.method == "POST" and request.path in ("/interrupt", "/api/interrupt") and request.can_read_body:
            try:
                data = await request.json()
            except json.JSONDecodeError:
                data = None
            prompt_id = data.get("prompt_id") if isinstance(data, dict) else None
            if isinstance(prompt_id, str) and prompt_id in cloud.jobs:
                await cloud.cancel(prompt_id)
                return web.Response()
        return await handler(request)

    server.app.middlewares.append(cloud_interrupt)

    @web.middleware
    async def cloud_model_lists(request, handler):
        # The GUI's model dropdowns also offer the models of every cloud endpoint. Read per request, so manifest edits show on reload.
        if not request.path.removeprefix("/api").startswith("/object_info"):
            return await handler(request)
        token = listed_models.set(model_lists(*(load_manifest(name) for name in endpoint_names())))
        try:
            return await handler(request)
        finally:
            listed_models.reset(token)

    server.app.middlewares.append(cloud_model_lists)


@server.routes.post("/cloud/prompt")
async def post_cloud_prompt(request):
    if cloud is None:
        return error_response(503, "Cloud Run is not configured", f"Set {', '.join(missing)} in {ENV_FILE}")
    return await cloud.post_prompt(request)


def restart_process():
    """Replace this process with a fresh ComfyUI with the same command line, as ComfyUI-Manager does."""
    logging.info("Cloud Run: restarting ComfyUI")
    if sys.platform == "win32":  # Windows execv doesn't quote the arguments
        os.execv(sys.executable, [f'"{sys.executable}"', f'"{sys.argv[0]}"', *sys.argv[1:]])
    os.execv(sys.executable, [sys.executable, *sys.argv])


@server.routes.post("/cloud/restart")
async def restart(request):
    # Running and queued prompts, local and cloud (cloud jobs sit in currently_running), would be lost.
    if server.prompt_queue.get_tasks_remaining():
        return error_response(409, "Prompts are running or queued", "Restart ComfyUI once the queue is empty.")
    asyncio.get_running_loop().call_later(0.5, restart_process)  # after the response is sent
    return web.json_response({})


@server.routes.get("/cloud/jobs")
async def cloud_jobs(request):
    return web.json_response({"configured": cloud is not None, "running": list(cloud.jobs) if cloud else []})


@server.routes.get("/cloud/bucket")
async def bucket_usage(request):
    if cloud is None or cloud.s3 is None:
        return web.json_response({"configured": False})
    return web.json_response({"configured": True, **await asyncio.to_thread(cloud.bucket_usage)})


@server.routes.post("/cloud/bucket/clear")
async def clear_bucket(request):
    if cloud is None or cloud.s3 is None:
        return error_response(400, "No bucket configured", f"Set the BUCKET_* lines in {ENV_FILE}")
    if cloud.jobs:
        return error_response(409, "Cloud jobs are running", "Their inputs and outputs are still needed. Clear the bucket when they are done.")
    return web.json_response({"deleted": await asyncio.to_thread(cloud.clear_bucket)})


@server.routes.post("/cloud/cancel")
async def cancel_cloud_jobs(request):
    """Cancel every running cloud job (the cloud Run group's cancel button)."""
    if cloud is not None:
        await asyncio.gather(*(cloud.cancel(prompt_id) for prompt_id in list(cloud.jobs)))
    return web.Response()


manage.add_routes(server.routes)
release.add_routes(server.routes, manifest)
