import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from release import next_tag, release_page_url


def test_next_tag():
    assert next_tag(None) == "v0.1.0"
    assert next_tag("v0.1.9") == "v0.1.10"


def test_release_page_url():
    url = release_page_url("git@github.com:tim/comfy_cloud_run.git", "v0.1.1", "abc")
    assert url == "https://github.com/tim/comfy_cloud_run/releases/new?tag=v0.1.1&target=abc&title=v0.1.1"
    assert release_page_url("https://github.com/tim/comfy_cloud_run", "v1.0.0", "abc").startswith("https://github.com/tim/comfy_cloud_run/releases/new?")
    assert release_page_url("git@gitlab.com:tim/x.git", "v1.0.0", "abc") is None
