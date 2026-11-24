"""Deploy the endpoints to the `cloud` branch and create the GitHub release that makes RunPod rebuild the images.

The current branch (normally main) is never changed: endpoints/ is untracked there. A deploy commits HEAD's tree plus the
working copy of endpoints/ onto the deploy branch, using a temporary index, and pushes only that branch.
"""
import asyncio
import os
import re
import shutil
import tempfile
import urllib.parse

from aiohttp import web

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEPLOY_BRANCH = "cloud"
SEMVER_TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)")
GITHUB_REMOTE = re.compile(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?")
lock = asyncio.Lock()


class GitError(Exception):
    pass


async def run(*args, env=None):
    proc = await asyncio.create_subprocess_exec(*args, cwd=PLUGIN_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
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


async def deploy_tree():
    """HEAD's tree with the working copy of endpoints/ in it, written through a temporary index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        await run("git", "read-tree", "HEAD", env=env)
        await run("git", "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", "endpoints", env=env)
        if os.path.isdir(os.path.join(PLUGIN_DIR, "endpoints")):
            await run("git", "add", "-f", "--", "endpoints", env=env)  # -f: endpoints/ is ignored on main
        return await run("git", "write-tree", env=env)


async def deployed_commit():
    """The deploy branch's commit on origin, fetched, or None before the first deploy."""
    if not await run("git", "ls-remote", "origin", f"refs/heads/{DEPLOY_BRANCH}"):
        return None
    await run("git", "fetch", "-q", "origin", f"+refs/heads/{DEPLOY_BRANCH}:refs/remotes/origin/{DEPLOY_BRANCH}")
    return await run("git", "rev-parse", f"refs/remotes/origin/{DEPLOY_BRANCH}")


async def is_ancestor(commit, of):
    try:
        await run("git", "merge-base", "--is-ancestor", commit, of)
        return True
    except GitError:
        return False


async def state():
    head = await run("git", "rev-parse", "HEAD")
    tree = await deploy_tree()
    deployed = await deployed_commit()
    if deployed:
        endpoint_changes = (await run("git", "diff", "--name-only", deployed, tree, "--", "endpoints")).splitlines()
        code_changes = not await is_ancestor(head, deployed)
    else:
        endpoint_changes = (await run("git", "ls-tree", "-r", "--name-only", tree, "--", "endpoints")).splitlines()
        code_changes = True
    tag, tag_commit = await latest_release()
    other = [line[3:] for line in (await run("git", "status", "--porcelain", "--untracked-files=all")).splitlines()]
    return {
        "head": head, "tree": tree, "deployed": deployed,
        "branch": await run("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "deploy_branch": DEPLOY_BRANCH,
        "endpoint_changes": endpoint_changes,
        "code_changes": code_changes,
        "released": deployed is not None and tag_commit == deployed,
        "other_changes": [path for path in other if not path.startswith("endpoints/")],
        "release": tag,
        "next_release": next_tag(tag),
        "gh": shutil.which("gh") is not None,
    }


async def release():
    """Commit and push the deploy branch if anything changed, then create the release (with gh) or return a prefilled release page URL."""
    s = await state()
    commit = s["deployed"]
    if s["endpoint_changes"] or s["code_changes"]:
        parents = ([s["deployed"]] if s["deployed"] else []) + ([s["head"]] if s["code_changes"] else [])
        endpoints = sorted({path.split("/")[1] for path in s["endpoint_changes"]})
        message = f"Deploy {s['branch']} {s['head'][:7]}" + (f" with endpoint{'s' if len(endpoints) > 1 else ''} {', '.join(endpoints)}" if endpoints else "")
        commit = await run("git", "commit-tree", s["tree"], *(arg for parent in parents for arg in ("-p", parent)), "-m", message)
        await run("git", "push", "origin", f"{commit}:refs/heads/{DEPLOY_BRANCH}")
    tag = s["next_release"]
    if s["gh"]:
        await run("gh", "release", "create", tag, "--target", commit, "--title", tag, "--generate-notes")
        return {"tag": tag, "published": True}
    url = release_page_url(await run("git", "remote", "get-url", "origin"), tag, commit)
    if url is None:
        raise GitError(f"Pushed {DEPLOY_BRANCH}, but origin is not on GitHub: create release {tag} by hand")
    return {"tag": tag, "published": False, "url": url}


def add_routes(routes):
    @routes.get("/cloud/release")
    async def get_status(request):
        try:
            s = await state()
        except (GitError, asyncio.TimeoutError) as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({k: v for k, v in s.items() if k not in ("head", "tree")})

    @routes.post("/cloud/release")
    async def post_release(request):
        if lock.locked():
            return web.json_response({"error": "A release is already in progress"}, status=409)
        async with lock:
            try:
                return web.json_response(await release())
            except (GitError, asyncio.TimeoutError) as e:
                return web.json_response({"error": str(e)}, status=500)
