"""Validate a prompt for a cloud endpoint: ComfyUI's own validation, with model lists read from the endpoint's manifest."""
import contextvars

import execution
import folder_paths
import nodes

NOT_MODEL_FOLDERS = {"configs", "custom_nodes"}
CORE_FILE_NODES = {"websocket_image_save"}  # shipped in ComfyUI's own custom_nodes folder, so the image has it too

manifest_models = contextvars.ContextVar("manifest_models", default=None)
local_filename_list = folder_paths.get_filename_list


def get_filename_list(folder_name):
    """Inside a cloud validation, model folders list the manifest's models instead of local files."""
    models = manifest_models.get()
    folder_name = folder_paths.map_legacy(folder_name)
    if models is None or folder_name in NOT_MODEL_FOLDERS:
        return local_filename_list(folder_name)
    return models.get(folder_name, [])


folder_paths.get_filename_list = get_filename_list


def missing_node_packs(prompt, manifest, endpoint):
    """node_errors for nodes from a locally installed pack that the manifest doesn't install."""
    packs = {n["url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower() for n in manifest["custom_nodes"]}
    node_errors = {}
    for node_id, node in prompt.items():
        class_def = nodes.NODE_CLASS_MAPPINGS.get(node.get("class_type"))
        parent, _, pack = (getattr(class_def, "RELATIVE_PYTHON_MODULE", None) or "").partition(".")
        if parent != "custom_nodes" or pack.lower() in packs or pack in CORE_FILE_NODES:
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
    models = {}
    for model in manifest["models"]:
        models.setdefault(folder_paths.map_legacy(model["directory"]), []).append(model["name"])
    token = manifest_models.set(models)
    try:
        return await execution.validate_prompt(prompt_id, prompt, partial_execution_targets)
    finally:
        manifest_models.reset(token)
