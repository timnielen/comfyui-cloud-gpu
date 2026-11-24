"""Commit manifest changes, push and create the GitHub release that makes RunPod rebuild the endpoint images."""
import asyncio
import os
import re
import shutil
import urllib.parse

from aiohttp import web

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

SEMVER_TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)")
GITHUB_REMOTE = re.compile(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")
lock = asyncio.Lock()


class GitError(Exception):
    pass


async def run(*args):
    proc = await asyncio.create_subprocess_exec(*args, cwd=PLUGIN_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    out, err = await asyncio.wait_for(proc.communicate(), 120)
    if proc.returncode != 0:
        raise GitError(f"{' '.join(args[:3])}: {(err or out).decode(errors='replace').strip()}")
    return out.decode().rstrip()  # not strip(): porcelain lines start with a space


async def latest_release():
    """(tag, commit) of the highest vX.Y.Z tag on origin, or (None, None)."""
    tags = []
    for line in (await run("git", "ls-remote", "--tags", "origin")).splitlines():
        sha, ref = line.split("\t")
        m = SEMVER_TAG.fullmatch(ref)
        if m:
            tags.append((tuple(map(int, m.groups())), sha))
    if not tags:
        return None, None
    version, sha = max(tags)
    return "v{}.{}.{}".format(*version), sha


def next_tag(tag):
    if tag is None:
        return "v0.1.0"
    major, minor, patch = map(int, tag[1:].split("."))
    return f"v{major}.{minor}.{patch + 1}"


def release_page_url(remote, tag, commit):
    """GitHub's new-release page with tag and target prefilled, or None if origin isn't on GitHub."""
    m = GITHUB_REMOTE.fullmatch(remote)
    if m is None:
        return None
    return f"https://github.com/{m.group(1)}/releases/new?" + urllib.parse.urlencode({"tag": tag, "target": commit, "title": tag})


async def status():
    changes = [line[3:] for line in (await run("git", "status", "--porcelain", "--untracked-files=all")).splitlines()]
    tag, tag_commit = await latest_release()
    unreleased = None
    if tag_commit:
        try:
            unreleased = int(await run("git", "rev-list", "--count", f"{tag_commit}..HEAD"))
        except GitError:  # the tagged commit isn't in the local clone
            pass
    return {
        "branch": await run("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "endpoint_changes": [c for c in changes if c.startswith("endpoints/")],
        "other_changes": [c for c in changes if not c.startswith("endpoints/")],
        "release": tag,
        "next_release": next_tag(tag),
        "commits_since_release": unreleased,
        "gh": shutil.which("gh") is not None,
    }


async def release():
    """Commit endpoint changes, push the branch, create the release (with gh) or return a prefilled release page URL."""
    before = await status()
    if before["endpoint_changes"]:
        await run("git", "add", "--", "endpoints")
        changed = sorted({path.split("/")[1] for path in before["endpoint_changes"]})
        await run("git", "commit", "-m", f"Update cloud endpoint{'s' if len(changed) > 1 else ''} {', '.join(changed)}")
    await run("git", "push", "-u", "origin", "HEAD")
    commit = await run("git", "rev-parse", "HEAD")
    tag = before["next_release"]
    if before["gh"]:
        await run("gh", "release", "create", tag, "--target", commit, "--title", tag, "--generate-notes")
        return {"tag": tag, "published": True}
    url = release_page_url(await run("git", "remote", "get-url", "origin"), tag, commit)
    if url is None:
        raise GitError(f"Pushed, but origin is not on GitHub: create release {tag} by hand")
    return {"tag": tag, "published": False, "url": url}


def add_routes(routes):
    @routes.get("/cloud/release")
    async def get_status(request):
        try:
            return web.json_response(await status())
        except (GitError, asyncio.TimeoutError) as e:
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/cloud/release")
    async def post_release(request):
        if lock.locked():
            return web.json_response({"error": "A release is already in progress"}, status=409)
        async with lock:
            try:
                return web.json_response(await release())
            except (GitError, asyncio.TimeoutError) as e:
                return web.json_response({"error": str(e)}, status=500)
