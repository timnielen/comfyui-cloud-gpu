import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { BUTTON, SIDEBAR_ID, VARIANT, cloud, el } from "./cloud_run.js";

// Manage endpoint manifests: models, node packs, the RunPod endpoint ID, and new endpoints.
const INPUT = "width: 100%; min-width: 0; box-sizing: border-box; background: var(--comfy-input-bg); color: var(--input-text); border: 1px solid var(--border-color); border-radius: 6px; padding: 4px 6px; font-size: 12px";
const MUTED = "font-size: 12px; color: var(--color-muted-foreground)";
const JSON_HEADERS = { "Content-Type": "application/json" };

const button = (text, onclick, variant = "secondary") => el("button", {
  type: "button", textContent: text, onclick, className: `${BUTTON} ${VARIANT[variant]}`,
  style: "height: 28px; padding: 0 10px; font-size: 12px; border-radius: 6px; flex-shrink: 0",
});
const row = (...children) => el("div", { style: "display: flex; gap: 6px; align-items: center" }, ...children);
const section = (title, ...children) => el("div", { style: "display: flex; flex-direction: column; gap: 6px; padding: 12px; border-bottom: 1px solid var(--border-color)" },
  el("div", { textContent: title, style: "font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--color-muted-foreground)" }), ...children);
const input = (props) => el("input", { style: INPUT, ...props });
const select = (options, value) => {
  const s = el("select", { style: INPUT }, ...options.map((o) => el("option", { value: o, textContent: o })));
  s.value = value;
  return s;
};
// A manifest entry: name, an image/runtime switch, remove. Runtime entries are left out of the image and installed by the
// worker when a job needs them, so changing them needs no rebuild.
function item(text, runtime, onToggle, onRemove) {
  const mode = button(runtime ? "runtime" : "image", onToggle, "textonly");
  mode.title = runtime ? "Installed by the worker when a job needs it. Click to bake it into the image." : "Baked into the image. Click to install it at run time instead.";
  mode.style.color = runtime ? "var(--color-muted-foreground)" : "";
  return row(el("span", { textContent: text, title: text, style: "flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px" }),
    mode, button("✕", onRemove, "textonly"));
}

const toggleRuntime = (entry) => { entry.runtime = !entry.runtime || undefined; };

function toast(detail, severity = "info") {
  app.extensionManager.toast.add({ severity, summary: "Cloud Run", detail, life: severity === "error" ? 8000 : 4000 });
}

async function request(route, method, body) {
  const r = await api.fetchApi(route, { method, headers: JSON_HEADERS, body: body && JSON.stringify(body) });
  if (!r.headers.get("Content-Type")?.includes("application/json")) {
    // The browser loads new plugin JavaScript on reload, the server only loads new routes on restart.
    if (r.status === 404 || r.status === 405) throw new Error(`The server doesn't know ${route.split("?")[0]} yet. Restart ComfyUI to finish the plugin update.`);
    throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error?.message ? `${data.error.message}. ${data.error.details}` : data.error);
  return data;
}

const fileName = (url) => decodeURIComponent(url.split(/[?#]/)[0].split("/").pop() ?? "");

let root = null;
let modelFolders = [];
let addablePacks = [];
let workflowMissing = null;
let creating = false;
let releaseStatus = null;
let bucket = null;
const FREE_TIER_BYTES = 10 * 1000 ** 3;  // Cloudflare R2's free storage
let releasing = false;

async function save(change) {
  const manifest = structuredClone(cloud.manifest);
  change(manifest);
  try {
    await request(`/cloud/endpoints/${cloud.selected}`, "PUT", manifest);
  } catch (e) {
    return toast(e.message, "error");
  }
  await cloud.refresh();
  await Promise.all([checkWorkflow(), loadReleaseStatus()]);
}

async function loadReleaseStatus() {
  releaseStatus = null;
  if (!cloud.selected) return render();
  const r = await api.fetchApi(`/cloud/release?endpoint=${encodeURIComponent(cloud.selected)}`);
  releaseStatus = r.headers.get("Content-Type")?.includes("application/json") ? await r.json() : { error: await r.text() };
  render();
}

const repoName = (origin) => origin.match(/github\.com[:/](.+?)(\.git)?\/?$/)?.[1] ?? origin;

async function publish() {
  const s = releaseStatus;
  const steps = [
    (s.image_changed || s.worker_changed) && `commit ${cloud.selected}'s Dockerfile and ${s.branch} to the ${s.deploy_branch} branch and push it to ${repoName(s.origin)}`,
    s.gh ? `create release ${s.next_release}` : `open GitHub to publish release ${s.next_release}`,
  ].filter(Boolean);
  const other = s.other_changes.length ? `\n\n${s.other_changes.length} uncommitted file(s) on ${s.branch} are not included.` : "";
  const ok = await app.extensionManager.dialog.confirm({
    title: `Update the ${cloud.selected} image`,
    message: `This will ${steps.join(", then ")}. ${s.branch} itself is not changed. RunPod then rebuilds the endpoint from that release.${other}`,
  });
  if (!ok) return;
  releasing = true;
  render();
  try {
    const result = await request(`/cloud/release?endpoint=${encodeURIComponent(cloud.selected)}`, "POST");
    if (result.published) toast(`Release ${result.tag} created. RunPod is rebuilding.`);
    else {
      window.open(result.url, "_blank");
      toast(`Pushed ${s.deploy_branch}. Publish release ${result.tag} on the GitHub page that just opened.`);
    }
  } catch (e) {
    toast(e.message, "error");
  }
  releasing = false;
  await loadReleaseStatus();
}

async function updatePlugin() {
  const s = releaseStatus;
  const ok = await app.extensionManager.dialog.confirm({
    title: "Update the plugin",
    message: `This will pull the plugin update from upstream into ${s.branch} and push ${s.branch} to ${repoName(s.origin)}. Nothing is deployed. Restart ComfyUI afterwards: the Cloud tab then shows for each endpoint whether the update changes its image.`,
  });
  if (!ok) return;
  releasing = true;
  render();
  try {
    await request("/cloud/update", "POST");
    toast("Plugin updated. Restart ComfyUI to use it.");
  } catch (e) {
    toast(e.message, "error");
  }
  releasing = false;
  await loadReleaseStatus();
}

async function restoreRelease() {
  const s = releaseStatus;
  const ok = await app.extensionManager.dialog.confirm({
    title: `Restore ${cloud.selected}`,
    message: `This discards the changes to ${cloud.selected}'s models, node packs, pip packages and ComfyUI version since release ${s.release}. The endpoint ID and ComfyUI options are kept.`,
  });
  if (!ok) return;
  try {
    const { tag } = await request(`/cloud/release/restore?endpoint=${encodeURIComponent(cloud.selected)}`, "POST");
    toast(`Restored ${cloud.selected} to ${tag}.`);
  } catch (e) {
    return toast(e.message, "error");
  }
  await cloud.refresh();
  await Promise.all([checkWorkflow(), loadReleaseStatus()]);
}

function workflowContents() {
  const graph = app.rootGraph ?? app.graph;
  const graphs = [graph, ...(graph.subgraphs?.values() ?? [])];
  const classTypes = new Set();
  const models = new Map();
  for (const g of graphs) {
    for (const node of g.nodes ?? []) {
      classTypes.add(node.type);
      for (const m of node.properties?.models ?? []) {
        if (m?.name && m.url && m.directory) models.set(`${m.directory}/${m.name}`, { name: m.name, directory: m.directory, url: m.url });
      }
    }
  }
  return { class_types: [...classTypes], models: [...models.values()] };
}

// What the open workflow needs that the selected endpoint lacks, and which installed node packs it could still get.
async function checkWorkflow() {
  if (cloud.selected) {
    [workflowMissing, addablePacks] = await Promise.all([
      request(`/cloud/endpoints/${cloud.selected}/missing`, "POST", workflowContents()),
      request(`/cloud/endpoints/${cloud.selected}/addable_node_packs`, "GET"),
    ]);
  } else {
    [workflowMissing, addablePacks] = [null, []];
  }
  render();
}

async function addNodePack(url, knownCommit) {
  try {
    const commit = knownCommit ?? (await request("/cloud/resolve_commit", "POST", { url })).commit;
    await save((m) => m.custom_nodes.push({ url, commit }));
  } catch (e) {
    toast(e.message, "error");
  }
}

// Handpicked ComfyUI options. They're sent with every job, and the worker starts ComfyUI with them, so no rebuild.
const OPTIONS = [
  ["--use-ck-attention", "Comfy Kitchen INT8 attention: faster video and large image models"],
  ["--fast fp16_accumulation", "fp16 accumulation: faster fp16 models, slightly lower precision"],
  ["--fast fp8_matrix_mult", "fp8 matrix math for fp8 models (RTX 40/50, L40, H100 and newer)"],
  ["--fast autotune", "cuDNN autotune: faster VAEs after the first run at a size"],
  ["--highvram", "Keep models in VRAM between runs instead of moving them to RAM"],
  ["--fp8_e4m3fn-text-enc", "Store text encoders in fp8 to save VRAM"],
  ["--cache-none", "Cache no node results: less RAM and VRAM, every node reruns"],
];

// Adds an option to the text, merging --fast features into one --fast: a second --fast would replace the first.
function addOption(text, option) {
  const tokens = text.split(/\s+/).filter(Boolean);
  const [flag, ...values] = option.split(" ");
  const at = tokens.indexOf(flag);
  if (at === -1) tokens.push(flag, ...values);
  else if (flag === "--fast" && tokens[at + 1] && !tokens[at + 1].startsWith("--") && !tokens.includes(values[0])) tokens.splice(at + 1, 0, ...values);
  return tokens.join(" ");
}

function optionsSection(manifest) {
  const text = input({ value: manifest.comfy_args ?? "", placeholder: "e.g. --use-ck-attention" });
  const picker = el("select", { style: INPUT }, el("option", { value: "", textContent: "Add an option…" }),
    ...OPTIONS.map(([value, textContent]) => el("option", { value, textContent, title: value })));
  picker.onchange = () => { text.value = addOption(text.value, picker.value); picker.value = ""; };
  return section("ComfyUI options", picker, row(text, button("Save", () => save((m) => { m.comfy_args = text.value.trim(); }))),
    el("div", { style: MUTED, textContent: "Command line options for the cloud ComfyUI. They apply to the next job without a rebuild; a warm worker restarts its ComfyUI once when they change." }));
}

function endpointSection() {
  if (creating) {
    const name = input({ placeholder: "name, e.g. flux" });
    const copy = el("input", { type: "checkbox" });
    const create = async () => {
      try {
        await request("/cloud/endpoints", "POST", { name: name.value.trim(), copy_from: copy.checked ? cloud.selected : null });
      } catch (e) {
        return toast(e.message, "error");
      }
      creating = false;
      await cloud.refresh();
      cloud.select(name.value.trim());
      await Promise.all([checkWorkflow(), loadReleaseStatus()]);
    };
    return section("New endpoint", name,
      el("label", { style: `${MUTED}; display: flex; gap: 6px; align-items: center` }, copy, `Copy models, nodes and ComfyUI options from ${cloud.selected ?? "–"}`),
      row(button("Create", create), button("Cancel", () => { creating = false; render(); }, "textonly")));
  }
  const choice = select(cloud.endpoints.map((e) => e.name), cloud.selected);
  choice.onchange = async () => { cloud.select(choice.value); await Promise.all([checkWorkflow(), loadReleaseStatus()]); };
  return section("Endpoint", row(choice, button("New…", () => { creating = true; render(); })));
}

function runpodSection(name, manifest) {
  const id = input({ value: manifest.endpoint_id, placeholder: "RunPod endpoint ID" });
  const children = [row(id, button("Save", () => save((m) => { m.endpoint_id = id.value.trim(); })))];
  if (!manifest.endpoint_id) {
    children.push(el("div", { style: MUTED, innerHTML: `Deploy it below first. Then create it in RunPod: Serverless → New Endpoint → GitHub repo, branch <code>cloud/${name}</code>, and paste its ID here.` }));
  }
  return section("RunPod endpoint", ...children);
}

function workflowSection() {
  if (!workflowMissing) return section("This workflow", el("div", { style: MUTED, textContent: "Checking…" }));
  const { node_packs: packs, models } = workflowMissing;
  const children = [];
  if (!packs.length && !models.length) children.push(el("div", { style: MUTED, textContent: "Everything this workflow lists is in the manifest." }));
  for (const pack of packs) {
    children.push(row(el("span", { textContent: `Node pack ${pack.name}`, style: "flex: 1; font-size: 12px" }),
      pack.url ? button("Add", () => addNodePack(pack.url, pack.commit)) : el("span", { style: MUTED, textContent: "add its git URL below" })));
  }
  for (const model of models) {
    children.push(row(el("span", { textContent: `${model.directory}/${model.name}`, title: model.url, style: "flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; font-size: 12px" }),
      button("Add", () => save((m) => m.models.push(model)))));
  }
  if (models.length > 1) children.push(button("Add all models", () => save((m) => m.models.push(...models))));
  children.push(el("div", { style: MUTED, textContent: "Models are found through the download links workflows embed in their loader nodes. Others can be added below." }));
  return section("Missing for this workflow", ...children, button("Check again", checkWorkflow, "textonly"));
}

function modelsSection(manifest) {
  const url = input({ placeholder: "Download URL" });
  const name = input({ placeholder: "File name" });
  const folder = select(modelFolders, "diffusion_models");
  url.oninput = () => { name.value = fileName(url.value); };
  const add = () => {
    if (!url.value.trim() || !name.value.trim()) return toast("Enter a download URL and a file name.", "warn");
    save((m) => m.models.push({ name: name.value.trim(), directory: folder.value, url: url.value.trim() }));
  };
  return section(`Models (${manifest.models.length})`,
    ...manifest.models.map((model, i) => item(`${model.directory}/${model.name}`, model.runtime,
      () => save((m) => toggleRuntime(m.models[i])), () => save((m) => m.models.splice(i, 1)))),
    url, row(folder, name), button("Add model", add));
}

function nodePacksSection(manifest) {
  const installed = addablePacks;
  const children = manifest.custom_nodes.map((node, i) =>
    item(`${fileName(node.url).replace(/\.git$/, "")} @ ${(node.commit ?? "latest").slice(0, 7)}`, node.runtime,
      () => save((m) => toggleRuntime(m.custom_nodes[i])), () => save((m) => m.custom_nodes.splice(i, 1))));
  if (installed.length) {
    const choice = select(installed.map((p) => p.name), installed[0].name);
    children.push(row(choice, button("Add installed", () => {
      const pack = installed.find((p) => p.name === choice.value);
      addNodePack(pack.url, pack.commit);
    })));
  }
  const url = input({ placeholder: "https://github.com/owner/repo" });
  children.push(row(url, button("Add", () => addNodePack(url.value.trim()))));
  return section(`Node packs (${manifest.custom_nodes.length})`, ...children);
}

async function loadBucket() {
  try {
    bucket = await request("/cloud/bucket", "GET");
  } catch (e) {
    bucket = { error: e.message };
  }
  render();
}

async function clearBucket() {
  const ok = await app.extensionManager.dialog.confirm({
    title: "Clear the bucket",
    message: "Delete all cloud inputs and outputs from the bucket? Outputs already downloaded to your output folder are kept. Inputs are uploaded again when needed.",
  });
  if (!ok) return;
  try {
    const { deleted } = await request("/cloud/bucket/clear", "POST");
    toast(`Deleted ${deleted} file(s) from the bucket.`);
  } catch (e) {
    toast(e.message, "error");
  }
  await loadBucket();
}

const formatBytes = (bytes) => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.ceil(bytes / 1e3)} KB`);

function bucketSection() {
  if (!bucket) return section("Bucket", el("div", { style: MUTED, textContent: "Checking…" }));
  if (bucket.error) return section("Bucket", el("div", { style: MUTED, textContent: bucket.error }), button("Check again", loadBucket, "textonly"));
  if (!bucket.configured) {
    return section("Bucket", el("div", { style: MUTED, textContent: "No bucket configured: files travel through RunPod directly, inputs up to about 7 MB per run. Add the BUCKET_* lines to .env for larger inputs." }));
  }
  const used = bucket.inputs.bytes + bucket.outputs.bytes;
  const share = Math.min(1, used / FREE_TIER_BYTES);
  const bar = el("div", { style: "height: 6px; border-radius: 3px; background: var(--comfy-input-bg); overflow: hidden" },
    el("div", { className: share > 0.9 ? "bg-destructive" : "bg-primary", style: `height: 100%; width: ${(share * 100).toFixed(1)}%` }));
  return section("Bucket",
    el("div", { style: "font-size: 12px", textContent: `${formatBytes(used)} of the 10 GB R2 free tier (${(share * 100).toFixed(1)}%)` }), bar,
    el("div", { style: MUTED, textContent: `Inputs ${formatBytes(bucket.inputs.bytes)} (${bucket.inputs.objects}) · outputs ${formatBytes(bucket.outputs.bytes)} (${bucket.outputs.objects})` }),
    row(button("Clear bucket", clearBucket), button("Refresh", loadBucket, "textonly")));
}

function releaseSection() {
  const s = releaseStatus;
  if (!s) return section("Cloud image", el("div", { style: MUTED, textContent: "Checking…" }));
  if (s.error) return section("Cloud image", el("div", { style: MUTED, textContent: s.error }));
  const pending = s.image_changed || s.worker_changed || !s.released;
  const lines = [`Repository: ${repoName(s.origin)}`, s.release ? `Latest release: ${s.release}` : "No release yet."];
  if (s.deployed && s.image_changed) lines.push("Models, node packs or the ComfyUI version changed since the last deploy.");
  if (s.worker_changed && s.deployed) lines.push(`The worker code on ${s.branch} changed since the last deploy.`);
  if (s.deployed && !s.image_changed && !s.worker_changed && !s.released) lines.push(`The ${s.deploy_branch} branch is ahead of the latest release.`);
  if (!pending) lines.push("The image is up to date.");
  if (s.other_changes.length) lines.push(`${s.other_changes.length} uncommitted file(s) on ${s.branch} won't be included.`);
  const go = button(releasing ? "Working…" : `Deploy & release ${s.next_release}`, publish);
  go.disabled = releasing || !pending;
  const children = [...lines.map((text) => el("div", { style: MUTED, textContent: text })), go];
  if (s.release && s.image_changed) {
    const restore = button(`Discard changes, back to ${s.release}`, restoreRelease, "textonly");
    restore.disabled = releasing;
    children.push(restore);
  }
  if (s.upstream_update) {
    const update = button(releasing ? "Working…" : "Update plugin", updatePlugin);
    update.disabled = releasing;
    children.push(el("div", { style: MUTED, textContent: "A plugin update is available upstream." }), update);
  }
  return section("Cloud image", ...children);
}

function render() {
  if (!root) return;
  const manifest = cloud.manifest;
  const content = [endpointSection()];
  if (manifest && !creating) {
    content.push(runpodSection(cloud.selected, manifest), workflowSection(), modelsSection(manifest), nodePacksSection(manifest), optionsSection(manifest), bucketSection(), releaseSection());
  }
  root.replaceChildren(el("div", { style: "display: flex; flex-direction: column; height: 100%; overflow-y: auto" }, ...content));
}

window.addEventListener("cloud-run:changed", render);

app.registerExtension({
  name: "comfy_cloud_run.Sidebar",
  setup() {
    app.extensionManager.registerSidebarTab({
      id: SIDEBAR_ID,
      icon: "pi pi-cloud",
      title: "Cloud",
      tooltip: "Cloud endpoints",
      type: "custom",
      render: async (element) => {
        root = element;
        render();
        [modelFolders] = await Promise.all([
          api.fetchApi("/models").then((r) => r.json()).then((f) => f.filter((x) => x !== "custom_nodes" && x !== "configs")),
          cloud.refresh(),
        ]);
        await Promise.all([checkWorkflow(), loadReleaseStatus(), loadBucket()]);
      },
      destroy: () => { root = null; },
    });
  },
});
