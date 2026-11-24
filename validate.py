"""Model lists for the cloud: validation against an endpoint's manifest, and the manifests' models in the GUI's dropdowns."""
import contextvars
import os
import re

import execution
import folder_paths
import nodes
from app.deploy_to_cloud import scan_custom_nodes
from .manifest import pack_name

NOT_MODEL_FOLDERS = {"configs", "custom_nodes"}
CORE_FILE_NODES = {"websocket_image_save"}  # shipped in ComfyUI's own custom_nodes folder, so the image has it too
PYPROJECT_REPOSITORY = re.compile(r'^\s*Repository\s*=\s*"([^"]+)"', re.MULTILINE)

manifest_models = contextvars.ContextVar("manifest_models", default=None)  # set while validating for an endpoint
listed_models = contextvars.ContextVar("listed_models", default=None)  # set while answering the GUI's model lists
local_filename_list = folder_paths.get_filename_list


def get_filename_list(folder_name):
    """Model folders list the manifest's models while validating for the cloud, and local plus cloud models for the GUI."""
    folder_name = folder_paths.map_legacy(folder_name)
    if folder_name in NOT_MODEL_FOLDERS:
        return local_filename_list(folder_name)
    models = manifest_models.get()
    if models is not None:
        return models.get(folder_name, [])
    listed = listed_models.get()
    if listed is None:
        return local_filename_list(folder_name)
    return sorted(set(local_filename_list(folder_name)) | set(listed.get(folder_name, [])))


folder_paths.get_filename_list = get_filename_list


def model_lists(*manifests):
    """{model folder: [names]} for the given manifests."""
    models = {}
    for manifest in manifests:
        for model in manifest["models"]:
            models.setdefault(folder_paths.map_legacy(model["directory"]), []).append(model["name"])
    return models


def node_pack(class_type):
    """The custom_nodes folder a node class comes from, or None for core nodes and files shipped with ComfyUI."""
    class_def = nodes.NODE_CLASS_MAPPINGS.get(class_type)
    parent, _, pack = (getattr(class_def, "RELATIVE_PYTHON_MODULE", None) or "").partition(".")
    return pack if parent == "custom_nodes" and pack not in CORE_FILE_NODES else None


def local_node_packs():
    """{folder name: {"url", "commit"}} for enabled local node packs whose repository is known: from their .git, or for
    packs installed from the Comfy Registry, the Repository entry of their pyproject.toml (commit unknown then)."""
    packs = {}
    for base in folder_paths.get_folder_paths("custom_nodes"):
        for pack in scan_custom_nodes([base]):
            url = pack.get("url")
            if url is None and pack["type"] == "cnr":
                with open(os.path.join(base, pack["dirName"], "pyproject.toml"), encoding="utf-8") as f:
                    m = PYPROJECT_REPOSITORY.search(f.read())
                url = m and m.group(1)
            if pack["enabled"] and url:
                packs[pack["dirName"]] = {"url": url, "commit": pack.get("commit")}
    return packs


def repo_key(url):
    """host/owner/repo of a git URL, so https, ssh and .git variants of one repository compare equal."""
    url = re.sub(r"^[a-z+]+://", "", url.strip().lower()).removesuffix("/").removesuffix(".git")
    return re.sub(r"^[^@/]+@", "", url).replace(":", "/", 1)


def manifest_entry(pack, manifest, local):
    """The manifest's node pack entry for a local pack folder, matched by repository URL or else by folder name.
    Registry installs often use another folder name than the repository, e.g. deno-custom-nodes."""
    url = local.get(pack, {}).get("url")
    for entry in manifest["custom_nodes"]:
        if pack_name(entry["url"]).lower() == pack.lower() or (url and repo_key(url) == repo_key(entry["url"])):
            return entry
    return None


def runtime_entries(prompt, manifest):
    """The manifest's runtime models and node packs this prompt uses: the worker installs them before running it."""
    values = {v for node in prompt.values() for v in node.get("inputs", {}).values() if isinstance(v, str)}
    local = local_node_packs()
    entries = [manifest_entry(pack, manifest, local) for pack in {node_pack(node.get("class_type")) for node in prompt.values()} if pack]
    return {"models": [m for m in manifest["models"] if m.get("runtime") and m["name"] in values],
            "custom_nodes": [n for n in manifest["custom_nodes"] if n.get("runtime") and any(n is e for e in entries)]}


def missing_node_packs(prompt, manifest, endpoint):
    """node_errors for nodes from a locally installed pack that the manifest doesn't install."""
    local = local_node_packs()
    node_errors = {}
    for node_id, node in prompt.items():
        pack = node_pack(node.get("class_type"))
        if pack is None or manifest_entry(pack, manifest, local):
            continue
        error = {"type": "cloud_missing_node_pack", "message": "Node pack is not in the cloud image",
                 "details": f"{pack}: add it to endpoints/{endpoint}/manifest.json", "extra_info": {}}
        node_errors[node_id] = {"errors": [error], "dependent_outputs": [], "class_type": node["class_type"]}
    return node_errors


async def validate_cloud_prompt(prompt_id, prompt, partial_execution_targets, manifest, endpoint):
    """Same result tuple as execution.validate_prompt: (ok, error, outputs to execute, node_errors)."""
    node_errors = missing_node_packs(prompt, manifest, endpoint)
    if node_errors:
        details = ", ".join(sorted({e["errors"][0]["details"].split(":")[0] for e in node_errors.values()}))
        error = {"type": "prompt_outputs_failed_validation", "message": "Node packs missing in the cloud image", "details": details, "extra_info": {}}
        return False, error, [], node_errors
    token = manifest_models.set(model_lists(manifest))
    try:
        return await execution.validate_prompt(prompt_id, prompt, partial_execution_targets)
    finally:
        manifest_models.reset(token)
