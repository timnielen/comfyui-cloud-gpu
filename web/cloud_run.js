import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Cloud runs go through the frontend's own queuePrompt, so batching, widget
// updates, job tracking and error display stay the same as for Run. The queue
// number is the only value that reaches the /prompt request body, so it marks
// a cloud run, and the request is sent to /cloud/prompt instead.
const CLOUD = "cloud";

const fetchApi = api.fetchApi.bind(api);
api.fetchApi = (route, options) => {
  if (route === "/prompt" && options?.method === "POST") {
    const body = JSON.parse(options.body);
    if (body.number === CLOUD) {
      delete body.number;
      return fetchApi("/cloud/prompt", { ...options, body: JSON.stringify(body) });
    }
  }
  return fetchApi(route, options);
};

const runOnCloud = () => app.queuePrompt(CLOUD, 1);

app.registerExtension({
  name: "comfy_cloud_run.RunOnCloud",
  commands: [{ id: "CloudRun.QueuePrompt", label: "Run on Cloud", icon: "pi pi-cloud-upload", function: runOnCloud }],
  keybindings: [{ combo: { key: "Enter", alt: true }, commandId: "CloudRun.QueuePrompt" }],
  actionBarButtons: [{ icon: "pi pi-cloud-upload", label: "Run on Cloud", tooltip: "Run on Cloud (Alt+Enter)", onClick: runOnCloud }],
});
