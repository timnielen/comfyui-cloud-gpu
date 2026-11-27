"""Deploy an endpoint to its branch, cloud/<name>, and create the GitHub release that makes RunPod rebuild its image.

The current branch (normally main) is never changed: endpoints/ is untracked there. A deploy commits HEAD's tree plus the
endpoint's generated Dockerfile at the root and its manifest, using a temporary index, and pushes only cloud/<name>.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

from aiohttp import web

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_REMOTE = re.compile(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")
lock = asyncio.Lock()


class GitError(Exception):
    pass


def deploy_branch(name):
    return f"cloud/{name}"


async def run(*args, env=None, stdin=None):
    proc = await asyncio.create_subprocess_exec(*args, cwd=PLUGIN_DIR, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    out, err = await asyncio.wait_for(proc.communicate(stdin), 120)
    if proc.returncode != 0:
        raise GitError(f"{' '.join(args[:3])}: {(err or out).decode(errors='replace').strip()}")
    return out.decode().rstrip()  # not strip(): porcelain lines start with a space


async def latest_release(name):
    """(tag, commit) of the endpoint's highest <name>-vX.Y.Z tag on origin, or (None, None)."""
    pattern = re.compile(rf"refs/tags/{re.escape(name)}-v(\d+)\.(\d+)\.(\d+)")
    tags = []
    for line in (await run("git", "ls-remote", "--tags", "origin")).splitlines():
        sha, ref = line.split("\t")
        m = pattern.fullmatch(ref)
        if m:
            tags.append((tuple(map(int, m.groups())), sha))
    if not tags:
        return None, None
    version, sha = max(tags)
    return "{}-v{}.{}.{}".format(name, *version), sha


def next_tag(name, tag):
    if tag is None:
        return f"{name}-v0.1.0"
    major, minor, patch = map(int, tag.removeprefix(f"{name}-v").split("."))
    return f"{name}-v{major}.{minor}.{patch + 1}"


def release_page_url(remote, tag, commit):
    """GitHub's new-release page with tag and target prefilled, or None if origin isn't on GitHub."""
    m = GITHUB_REMOTE.fullmatch(remote)
    if m is None:
        return None
    return f"https://github.com/{m.group(1)}/releases/new?" + urllib.parse.urlencode({"tag": tag, "target": commit, "title": tag})


async def deploy_tree(name, dockerfile):
    """HEAD's tree plus the endpoint's Dockerfile at the root and endpoints/<name>/manifest.json, via a temporary index."""
    manifest_path = f"endpoints/{name}/manifest.json"
    blob = await run("git", "hash-object", "-w", "--stdin", stdin=dockerfile.encode())
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        await run("git", "read-tree", "HEAD", env=env)
        await run("git", "update-index", "--add", "--cacheinfo", f"100644,{blob},Dockerfile", env=env)
        await run("git", "add", "-f", "--", manifest_path, env=env)  # -f: endpoints/ is ignored on main
        return await run("git", "write-tree", env=env)


async def deployed_commit(name):
    """The endpoint branch's commit on origin, fetched, or None before the first deploy.

    Fetched from origin's URL into refs/cloud-run/: fetching by remote name would also update refs/remotes/origin/,
    where a stale origin/cloud ref (or any branch named like a prefix of cloud/<name>) blocks creating origin/cloud/<name>.
    """
    branch = deploy_branch(name)
    if not await run("git", "ls-remote", "origin", f"refs/heads/{branch}"):
        return None
    url = await run("git", "remote", "get-url", "origin")
    await run("git", "fetch", "-q", "--no-write-fetch-head", url, f"+refs/heads/{branch}:refs/cloud-run/{name}")
    return await run("git", "rev-parse", f"refs/cloud-run/{name}")


async def restore_endpoints(names, save_manifest):
    """Restore the manifests of origin's cloud/<name> branches that have no local endpoint folder.

    endpoints/ is ignored on main, so a fresh clone has none; each deploy branch carries its endpoint's manifest.
    """
    restored = []
    for line in (await run("git", "ls-remote", "origin", "refs/heads/cloud/*")).splitlines():
        name = line.split("\t")[1].removeprefix("refs/heads/cloud/")
        if name in names:
            continue
        try:
            await deployed_commit(name)
            text = await run("git", "show", f"refs/cloud-run/{name}:endpoints/{name}/manifest.json")
        except GitError:  # not a deploy branch of this plugin
            continue
        try:
            save_manifest(name, json.loads(text))
        except ValueError as e:  # bad JSON, manifest or endpoint name
            logging.warning(f"Cloud Run: could not restore endpoint {name!r} from its branch: {e}")
            continue
        restored.append(name)
    return restored


async def released_manifest(name):
    """(tag, manifest) of the endpoint's latest release, fetched from origin into refs/cloud-run-release/<name>."""
    tag, _ = await latest_release(name)
    if tag is None:
        raise GitError(f"{name} has no release yet")
    url = await run("git", "remote", "get-url", "origin")
    await run("git", "fetch", "-q", "--no-write-fetch-head", url, f"+refs/tags/{tag}:refs/cloud-run-release/{name}")
    return tag, json.loads(await run("git", "show", f"refs/cloud-run-release/{name}:endpoints/{name}/manifest.json"))


async def is_ancestor(commit, of):
    try:
        await run("git", "merge-base", "--is-ancestor", commit, of)
        return True
    except GitError:
        return False


async def upstream_update(branch, head):
    """Whether the upstream remote (the plugin's original repo, see the README) has commits for `branch` that HEAD lacks."""
    if "upstream" not in (await run("git", "remote")).splitlines():
        return False
    try:
        line = await run("git", "ls-remote", "upstream", f"refs/heads/{branch}")
    except (GitError, asyncio.TimeoutError):  # offline, or no such branch upstream: nothing to offer
        return False
    # An upstream commit not fetched yet isn't an ancestor either.
    return bool(line) and line.split("\t")[0] != head and not await is_ancestor(line.split("\t")[0], head)


async def state(name, dockerfile):
    head = await run("git", "rev-parse", "HEAD")
    branch = await run("git", "rev-parse", "--abbrev-ref", "HEAD")
    tree = await deploy_tree(name, dockerfile)
    deployed = await deployed_commit(name)
    # Only the Dockerfile and the plugin code shape the image. The manifest copy on the branch is just for restoring an
    # endpoint elsewhere, so manifest-only edits (e.g. the endpoint ID) wait for the next real deploy.
    if deployed:
        image_changed = bool(await run("git", "diff", "--name-only", deployed, tree, "--", "Dockerfile"))
        code_changes = not await is_ancestor(head, deployed)
    else:
        image_changed = code_changes = True
    tag, tag_commit = await latest_release(name)
    other = [line[3:] for line in (await run("git", "status", "--porcelain", "--untracked-files=all")).splitlines()]
    return {
        "head": head, "tree": tree, "deployed": deployed,
        "branch": branch,
        "upstream_update": await upstream_update(branch, head),
        "deploy_branch": deploy_branch(name),
        "image_changed": image_changed,
        "code_changes": code_changes,
        "released": deployed is not None and tag_commit == deployed,
        "other_changes": [path for path in other if not path.startswith("endpoints/")],
        "release": tag,
        "next_release": next_tag(name, tag),
        "gh": shutil.which("gh") is not None,
        "origin": await run("git", "remote", "get-url", "origin"),
        "push_url": await run("git", "remote", "get-url", "--push", "origin"),
    }


async def release(name, dockerfile):
    """Commit and push cloud/<name> if anything changed, then create the release (with gh) or return a prefilled release page URL."""
    s = await state(name, dockerfile)
    commit = s["deployed"]
    if s["image_changed"] or s["code_changes"]:
        parents = ([s["deployed"]] if s["deployed"] else []) + ([s["head"]] if s["code_changes"] else [])
        message = f"Deploy {name} from {s['branch']} {s['head'][:7]}"
        commit = await run("git", "commit-tree", s["tree"], *(arg for parent in parents for arg in ("-p", parent)), "-m", message)
        # Pushing to the URL, not the remote name, keeps git from also updating refs/remotes/origin/ (see deployed_commit).
        await run("git", "push", s["push_url"], f"{commit}:refs/heads/{s['deploy_branch']}")
    tag = s["next_release"]
    if s["gh"]:
        await run("gh", "release", "create", tag, "--target", commit, "--title", tag, "--generate-notes")
        return {"tag": tag, "published": True}
    url = release_page_url(await run("git", "remote", "get-url", "origin"), tag, commit)
    if url is None:
        raise GitError(f"Pushed {s['deploy_branch']}, but origin is not on GitHub: create release {tag} by hand")
    return {"tag": tag, "published": False, "url": url}


async def update(name):
    """Fast-forward the current branch from upstream, push it to origin, then deploy and release the endpoint."""
    branch = await run("git", "rev-parse", "--abbrev-ref", "HEAD")
    try:
        await run("git", "pull", "-q", "--ff-only", "upstream", branch)
    except GitError as e:
        raise GitError(f"{str(e).splitlines()[-1]} Pull by hand: git pull upstream {branch}") from None
    await run("git", "push", "-q", "origin", branch)
    # The pulled code may generate a different Dockerfile than the one this (not yet restarted) process would.
    await asyncio.to_thread(subprocess.run, [sys.executable, os.path.join(PLUGIN_DIR, "tools", "gen_dockerfile.py"), name],
                            cwd=PLUGIN_DIR, check=True, capture_output=True)
    with open(os.path.join(PLUGIN_DIR, "endpoints", name, "Dockerfile"), encoding="utf-8") as f:
        return await release(name, f.read())


def add_routes(routes, manifests):
    """`manifests` is the plugin's manifest module, passed in so this file imports without the plugin package (tests)."""
    def endpoint(request):
        name = request.query.get("endpoint", "")
        if name not in manifests.endpoint_names():
            raise web.HTTPNotFound(text=f"Unknown endpoint {name!r}")
        return name, manifests.dockerfile(manifests.load_manifest(name))

    @routes.get("/cloud/release")
    async def get_status(request):
        try:
            s = await state(*endpoint(request))
        except (GitError, asyncio.TimeoutError) as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({k: v for k, v in s.items() if k not in ("head", "tree", "push_url")})

    @routes.post("/cloud/release")
    async def post_release(request):
        name, dockerfile = endpoint(request)
        if lock.locked():
            return web.json_response({"error": "A release is already in progress"}, status=409)
        async with lock:
            try:
                return web.json_response(await release(name, dockerfile))
            except (GitError, asyncio.TimeoutError) as e:
                return web.json_response({"error": str(e)}, status=500)

    @routes.post("/cloud/endpoints/restore")
    async def post_restore(request):
        if lock.locked():
            return web.json_response({"restored": []})
        async with lock:
            try:
                return web.json_response({"restored": await restore_endpoints(manifests.endpoint_names(), manifests.save_manifest)})
            except (GitError, asyncio.TimeoutError) as e:
                return web.json_response({"error": str(e)}, status=500)

    @routes.post("/cloud/release/restore")
    async def post_restore_release(request):
        """Discard the endpoint's image changes since its latest release. The endpoint ID and ComfyUI options aren't part of
        the image (and only reach the branch with a deploy), so the local ones are kept."""
        name, _ = endpoint(request)
        async with lock:
            try:
                tag, released = await released_manifest(name)
            except (GitError, asyncio.TimeoutError) as e:
                return web.json_response({"error": str(e)}, status=500)
        current = manifests.load_manifest(name)
        manifests.save_manifest(name, {**released, "endpoint_id": current["endpoint_id"], "comfy_args": current.get("comfy_args", "")})
        return web.json_response({"tag": tag})

    @routes.post("/cloud/update")
    async def post_update(request):
        name, _ = endpoint(request)
        if lock.locked():
            return web.json_response({"error": "A release is already in progress"}, status=409)
        async with lock:
            try:
                return web.json_response(await update(name))
            except (GitError, asyncio.TimeoutError) as e:
                return web.json_response({"error": str(e)}, status=500)
            except subprocess.CalledProcessError as e:
                return web.json_response({"error": f"Pulled the update, but generating the Dockerfile failed: {e.stderr.decode(errors='replace').strip()}"}, status=500)
