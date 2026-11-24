"""Run on Cloud: run prompts from the local GUI on a RunPod Serverless endpoint. The cloud worker itself lives in worker/."""
import asyncio
import json
import os

from aiohttp import web

from server import PromptServer
from .cloud import CloudRun, error_response
from .manage import add_routes
from .manifest import PLUGIN_DIR, endpoint_names, load_manifest
from .validate import listed_models, model_lists

ENV_KEYS = ("RUNPOD_API_KEY", "BUCKET_ENDPOINT", "BUCKET_NAME", "BUCKET_KEY", "BUCKET_SECRET")
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
missing = [k for k in ENV_KEYS if not env.get(k)]
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


@server.routes.get("/cloud/jobs")
async def cloud_jobs(request):
    return web.json_response({"configured": cloud is not None, "running": list(cloud.jobs) if cloud else []})


@server.routes.post("/cloud/cancel")
async def cancel_cloud_jobs(request):
    """Cancel every running cloud job (the Run on Cloud group's cancel button)."""
    if cloud is not None:
        await asyncio.gather(*(cloud.cancel(prompt_id) for prompt_id in list(cloud.jobs)))
    return web.Response()


add_routes(server.routes)
