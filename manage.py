"""Routes for managing endpoint manifests from the GUI. They only touch files in this repo, so they work without credentials."""
import asyncio
import os

from aiohttp import web

import folder_paths
from .manifest import endpoint_dir, endpoint_names, load_manifest, new_manifest, pack_name, save_manifest
from .validate import local_node_packs, manifest_entry, node_pack


COMFY_DIR = os.path.dirname(os.path.abspath(folder_paths.__file__))  # the running ComfyUI, wherever custom_nodes lives


def error(status, message):
    return web.json_response({"error": message}, status=status)


def add_routes(routes):
    @routes.get("/cloud/endpoints")
    async def list_endpoints(request):
        return web.json_response([{"name": name, "manifest": load_manifest(name)} for name in endpoint_names()])

    @routes.post("/cloud/endpoints")
    async def create_endpoint(request):
        body = await request.json()
        name, copy_from = body.get("name", ""), body.get("copy_from")
        try:
            if os.path.exists(endpoint_dir(name)):
                return error(409, f"Endpoint {name!r} already exists")
            if copy_from and copy_from not in endpoint_names():
                return error(404, f"Unknown endpoint {copy_from!r}")
            manifest = {**load_manifest(copy_from), "endpoint_id": ""} if copy_from else await asyncio.to_thread(new_manifest, COMFY_DIR)
            save_manifest(name, manifest)
        except ValueError as e:
            return error(400, str(e))
        return web.json_response({"name": name, "manifest": load_manifest(name)})

    @routes.put("/cloud/endpoints/{name}")
    async def update_endpoint(request):
        name = request.match_info["name"]
        if name not in endpoint_names():
            return error(404, f"Unknown endpoint {name!r}")
        try:
            save_manifest(name, await request.json())
        except ValueError as e:
            return error(400, str(e))
        return web.json_response({"name": name, "manifest": load_manifest(name)})

    @routes.post("/cloud/endpoints/{name}/missing")
    async def missing_for_workflow(request):
        """What a workflow needs that the endpoint's manifest lacks. Body: {"class_types": [...], "models": [{"name", "directory", "url"}]}."""
        name = request.match_info["name"]
        if name not in endpoint_names():
            return error(404, f"Unknown endpoint {name!r}")
        body = await request.json()
        manifest = load_manifest(name)
        local = await asyncio.to_thread(local_node_packs)
        packs = sorted({pack for pack in map(node_pack, body.get("class_types", [])) if pack and not manifest_entry(pack, manifest, local)})
        have_models = {(folder_paths.map_legacy(m["directory"]), m["name"]) for m in manifest["models"]}
        models = [m for m in body.get("models", []) if (folder_paths.map_legacy(m["directory"]), m["name"]) not in have_models]
        return web.json_response({"node_packs": [{"name": p, **local.get(p, {"url": None, "commit": None})} for p in packs], "models": models})

    @routes.get("/cloud/endpoints/{name}/addable_node_packs")
    async def addable_node_packs(request):
        """Locally installed node packs with a known repository that the endpoint's manifest doesn't have yet."""
        name = request.match_info["name"]
        if name not in endpoint_names():
            return error(404, f"Unknown endpoint {name!r}")
        manifest = load_manifest(name)
        packs = await asyncio.to_thread(local_node_packs)
        return web.json_response([{"name": pack, **info} for pack, info in sorted(packs.items())
                                  if pack != "comfy_cloud_run" and not manifest_entry(pack, manifest, packs)])

    @routes.post("/cloud/resolve_commit")
    async def resolve_commit(request):
        """Pin a node pack: the current HEAD commit of a git URL."""
        url = (await request.json()).get("url", "")
        if not url.startswith("https://"):
            return error(400, "Only https:// git URLs are supported")
        proc = await asyncio.create_subprocess_exec("git", "ls-remote", url, "HEAD", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            return error(504, f"git ls-remote {url} timed out")
        if proc.returncode != 0 or not out.split():
            return error(400, f"Could not read {url}: {err.decode(errors='replace').strip()}")
        return web.json_response({"url": url, "name": pack_name(url), "commit": out.split()[0].decode()})
