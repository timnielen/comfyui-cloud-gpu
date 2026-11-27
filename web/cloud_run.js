import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

export const SIDEBAR_ID = "comfy-cloud-run";
// Class strings of the frontend's Button component (button.variants.ts), so the cloud group matches the Run group.
export const BUTTON = "relative inline-flex cursor-pointer touch-manipulation appearance-none items-center justify-center gap-2 rounded-md border-none font-inter text-sm font-medium whitespace-nowrap transition-colors focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50";
export const VARIANT = {
  secondary: "bg-secondary text-foreground hover:bg-accent",
  destructive: "bg-destructive text-foreground hover:bg-destructive/90",
  textonly: "bg-transparent text-foreground hover:bg-accent",
};

export function el(tag, props = {}, ...children) {
  const element = Object.assign(document.createElement(tag), props);
  element.append(...children);
  return element;
}

// Endpoints from endpoints/*/manifest.json and the one cloud runs use. Shared with the sidebar.
export const cloud = {
  endpoints: [],
  selected: localStorage.getItem("CloudRun.endpoint"),
  running: [],
  async refresh() {
    this.endpoints = await api.fetchApi("/cloud/endpoints").then((r) => r.json());
    if (!this.endpoints.some((e) => e.name === this.selected)) this.selected = this.endpoints[0]?.name ?? null;
    window.dispatchEvent(new CustomEvent("cloud-run:changed"));
  },
  select(name) {
    this.selected = name;
    localStorage.setItem("CloudRun.endpoint", name);
    window.dispatchEvent(new CustomEvent("cloud-run:changed"));
  },
  get manifest() {
    return this.endpoints.find((e) => e.name === this.selected)?.manifest;
  },
};

// Cloud runs go through the frontend's own queuePrompt, so batching, widget updates, job tracking and error display
// stay the same as for Run. The queue number is the only value that reaches the /prompt request body, so it marks a
// cloud run, and the request is sent to /cloud/prompt instead.
const CLOUD = "cloud";
const fetchApi = api.fetchApi.bind(api);
api.fetchApi = (route, options) => {
  if (route === "/prompt" && options?.method === "POST") {
    const body = JSON.parse(options.body);
    if (body.number === CLOUD) {
      delete body.number;
      body.cloud_endpoint = cloud.selected;
      return fetchApi("/cloud/prompt", { ...options, body: JSON.stringify(body) });
    }
  }
  return fetchApi(route, options);
};

const runOnCloud = () => app.queuePrompt(CLOUD, 1);
const openSidebar = () => app.extensionManager.command.execute(`Workspace.ToggleSidebarTab.${SIDEBAR_ID}`);

// The cloud Run group lives in the frontend's Run widget, next to its interrupt button, so it moves and docks with it.
const label = el("span");
const runButton = el("button", { type: "button", title: "Run in the cloud (Alt+Enter)", onclick: runOnCloud },
  el("i", { className: "pi pi-cloud-upload", style: "font-size: 14px" }), label);
const menuButton = el("button", { type: "button", title: "Cloud endpoint", onclick: () => toggleMenu() });
const group = el("div", {}, runButton, menuButton);
const cancelButton = el("button", { type: "button", title: "Cancel running cloud jobs", onclick: () => api.fetchApi("/cloud/cancel", { method: "POST" }) },
  el("i", { className: "icon-[lucide--x] size-4" }));

function setIfChanged(element, key, value) {
  if (element[key] !== value) element[key] = value;
}

function sync() {
  const run = document.querySelector('[data-testid="queue-button"]');
  const trigger = document.querySelector('[data-testid="queue-mode-menu-trigger"]');
  const row = run?.closest('[data-testid="actionbar-panel"]')?.firstElementChild;
  if (!row || !trigger) return;
  setIfChanged(group, "className", run.parentElement.className);
  setIfChanged(runButton, "className", run.className);
  setIfChanged(menuButton, "className", trigger.className);
  setIfChanged(menuButton, "innerHTML", trigger.innerHTML);
  setIfChanged(label, "textContent", cloud.selected ? `Cloud · ${cloud.selected}` : "Cloud");
  setIfChanged(cancelButton, "className", `${BUTTON} ${cloud.running.length ? VARIANT.destructive : VARIANT.secondary} size-8 rounded-lg`);
  setIfChanged(cancelButton, "disabled", !cloud.running.length);
  const interrupt = [...row.children].find((child) => child.contains(run))?.nextElementSibling;
  if (interrupt && interrupt.nextElementSibling !== group) interrupt.after(group, cancelButton);
}

let menu = null;
function closeMenu() {
  menu?.remove();
  menu = null;
}
function menuItem(text, onclick) {
  return el("button", {
    type: "button", textContent: text, className: `${BUTTON} ${VARIANT.textonly}`,
    style: "width: 100%; height: 30px; justify-content: flex-start; padding: 0 10px; border-radius: 6px",
    onclick: () => { closeMenu(); onclick(); },
  });
}
function toggleMenu() {
  if (menu) return closeMenu();
  const anchor = menuButton.getBoundingClientRect();
  menu = el("div", {
    className: "bg-secondary text-foreground",
    style: `position: fixed; z-index: 10000; top: ${anchor.bottom + 4}px; left: ${Math.max(8, anchor.right - 220)}px; width: 220px;
            padding: 4px; border-radius: 8px; border: 1px solid var(--border-color); box-shadow: 0 6px 20px rgba(0, 0, 0, 0.4)`,
  }, el("div", { textContent: "Cloud endpoint", style: "padding: 4px 10px; font-size: 11px; color: var(--color-muted-foreground)" }));
  for (const endpoint of cloud.endpoints) {
    const mark = endpoint.name === cloud.selected ? "✓ " : " ";
    const missingId = endpoint.manifest.endpoint_id ? "" : "  (no endpoint ID)";
    menu.append(menuItem(`${mark}${endpoint.name}${missingId}`, () => cloud.select(endpoint.name)));
  }
  menu.append(el("div", { style: "height: 1px; margin: 4px 0; background: var(--border-color)" }), menuItem("Manage endpoints…", openSidebar));
  document.body.append(menu);
}
document.addEventListener("pointerdown", (e) => {
  if (menu && !menu.contains(e.target) && !menuButton.contains(e.target)) closeMenu();
}, true);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeMenu());

async function updateRunning() {
  cloud.running = (await api.fetchApi("/cloud/jobs").then((r) => r.json())).running;
  sync();
}

let syncQueued = false;
new MutationObserver(() => {
  if (syncQueued) return;
  syncQueued = true;
  requestAnimationFrame(() => { syncQueued = false; sync(); });
}).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });

window.addEventListener("cloud-run:changed", sync);
api.addEventListener("status", updateRunning);

// Endpoints that are deployed but missing here, e.g. in a fresh clone, come back from their cloud/<name> branches.
async function restoreEndpoints() {
  const r = await api.fetchApi("/cloud/endpoints/restore", { method: "POST" });
  const { restored, error } = await r.json();
  if (error) return console.warn(`Cloud Run: could not restore endpoints: ${error}`);
  if (!restored.length) return;
  await cloud.refresh();
  app.extensionManager.toast.add({ severity: "info", summary: "Cloud Run", detail: `Restored ${restored.join(", ")} from the repository's cloud branches.`, life: 6000 });
}

app.registerExtension({
  name: "comfy_cloud_run.RunOnCloud",
  commands: [{ id: "CloudRun.QueuePrompt", label: "Run in the cloud", icon: "pi pi-cloud-upload", function: runOnCloud }],
  keybindings: [{ combo: { key: "Enter", alt: true }, commandId: "CloudRun.QueuePrompt" }],
  async setup() {
    await cloud.refresh();
    await updateRunning();
    await restoreEndpoints();
  },
});
