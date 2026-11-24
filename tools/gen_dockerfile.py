"""Write endpoints/<name>/Dockerfile from endpoints/<name>/manifest.json.

Usage: python tools/gen_dockerfile.py h3 [--pin-local]
--pin-local first sets the manifest's ComfyUI commit to the ComfyUI checkout this plugin is installed in,
so the cloud runs the same ComfyUI version as the local GUI. The commit must exist at the manifest's ComfyUI url.
The image is built with this plugin's repository root as build context.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manifest import endpoint_dir, load_manifest, local_comfyui_commit, save_manifest


def main():
    name = sys.argv[1]
    manifest = load_manifest(name)
    if "--pin-local" in sys.argv[2:]:
        manifest["comfyui"]["commit"] = local_comfyui_commit()
        print(f"pinned ComfyUI to {manifest['comfyui']['commit']}")
    save_manifest(name, manifest)
    print(f"wrote {os.path.join(endpoint_dir(name), 'Dockerfile')}")


if __name__ == "__main__":
    main()
