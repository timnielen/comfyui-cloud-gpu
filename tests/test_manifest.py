import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from manifest import check_manifest, dockerfile, format_manifest

MANIFEST = {
    "endpoint_id": "",
    "comfyui": {"url": "https://github.com/Comfy-Org/ComfyUI", "commit": "c0ffee"},
    "custom_nodes": [{"url": "https://github.com/a/NodePack.git", "commit": "abc123"}, {"url": "https://github.com/b/Other/"}],
    "pip": ["transformers<5"],
    "models": [{"name": "it's.safetensors", "directory": "vae", "url": "https://example.com/m?x=1&y=2"}],
}


def test_layer_order():
    text = dockerfile(MANIFEST)
    order = [text.index(s) for s in ("curl -fsSL", "pip install torch", "fetch -q --depth 1 https://github.com/Comfy-Org/ComfyUI c0ffee",
                                      "pip install -r requirements.txt", "custom_nodes/NodePack", "COPY worker /worker")]
    assert order == sorted(order)
    assert text.rstrip().endswith('CMD ["python", "/worker/handler.py"]')


def test_quoting_and_pinning():
    text = dockerfile(MANIFEST)
    assert "-o 'vae/it'\"'\"'s.safetensors' 'https://example.com/m?x=1&y=2'" in text
    assert "fetch -q --depth 1 https://github.com/a/NodePack.git abc123" in text
    assert "git clone -q --depth 1 https://github.com/b/Other/ custom_nodes/Other" in text
    assert "RUN pip install 'transformers<5'" in text


def test_format_manifest_round_trips():
    text = format_manifest(MANIFEST)
    assert json.loads(text) == MANIFEST
    assert '\n    {"url": "https://github.com/a/NodePack.git", "commit": "abc123"},\n' in text


@pytest.mark.parametrize("directory", ["../escape", "/abs", "a/../b", "back\\slash"])
def test_check_manifest_rejects_unsafe_model_paths(directory):
    with pytest.raises(ValueError):
        check_manifest({**MANIFEST, "models": [{"name": "m.safetensors", "directory": directory, "url": "https://x"}]})


def test_check_manifest_accepts_valid():
    check_manifest({**MANIFEST, "models": [{"name": "sub dir/m (1).safetensors", "directory": "diffusion_models", "url": "https://x"}]})


def test_runtime_entries_stay_out_of_the_image():
    manifest = {**MANIFEST,
                "custom_nodes": [{"url": "https://github.com/a/Baked"}, {"url": "https://github.com/a/Live", "runtime": True}],
                "models": [{"name": "baked.safetensors", "directory": "vae", "url": "https://x/baked"},
                           {"name": "live.safetensors", "directory": "loras", "url": "https://x/live", "runtime": True}]}
    text = dockerfile(manifest)
    assert "custom_nodes/Baked" in text and "baked.safetensors" in text
    assert "Live" not in text and "live.safetensors" not in text
    check_manifest(manifest)


def test_check_manifest_rejects_non_boolean_runtime():
    with pytest.raises(ValueError):
        check_manifest({**MANIFEST, "models": [{"name": "m.safetensors", "directory": "vae", "url": "https://x", "runtime": "yes"}]})
