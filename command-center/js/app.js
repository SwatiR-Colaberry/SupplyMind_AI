import { loadData } from "./data.js";
import { formatAbsoluteDate, relativeAge, ageInDays } from "./format.js";
import { renderOverview } from "./tabs/overview.js";
import { renderStub } from "./tabs/stub.js";

const TABS = [
  { key: "overview", label: "Overview", built: true },
  { key: "outcomes", label: "Outcomes", built: false, title: "Outcomes", blurb: "The numbers this project is meant to move." },
  { key: "users", label: "Users & Use Case", built: false, title: "Users and Use Case", blurb: "Who this is for and what they're trying to get done." },
  { key: "guardrails", label: "Guardrails", built: false, title: "Guardrails", blurb: "What must never happen." },
  { key: "systems", label: "Systems", built: false, title: "Systems", blurb: "What this connects to." },
  { key: "pm", label: "Project Management", built: false, title: "Project Management", blurb: "Releases, schedule, and every task's status." },
  { key: "agents", label: "AI Agents", built: false, title: "AI Agents", blurb: "The agent roster and what each one owns." },
  { key: "kb", label: "Knowledge Base", built: false, title: "Knowledge Base", blurb: "Requirements, stories, traceability, and a chat panel over this project's own data." },
  { key: "data-model", label: "Data Model", built: false, title: "Data Model", blurb: "The tables behind all of the above." },
];

const MODE_KEY = "cc-mode";
function getMode() {
  return localStorage.getItem(MODE_KEY) === "sample" ? "sample" : "real";
}
function setMode(mode) {
  localStorage.setItem(MODE_KEY, mode);
}

function currentRoute() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const [key, ...rest] = hash.split("/").filter(Boolean);
  return { key: key || "overview", rest };
}

function renderHeader(root, ctx) {
  const { manifest } = ctx.data;
  const generatedAt = manifest?.generated_at;
  const age = ageInDays(generatedAt);
  const isWarn = age > 7;

  const header = document.createElement("header");
  header.className = "cc-header";
  header.innerHTML = `
    <div class="cc-header-top">
      <div class="cc-brand">
        <div class="cc-brand-name">${ctx.data.plan?.project?.name || "SupplyMind AI"}</div>
        <div class="cc-brand-desc">${ctx.data.plan?.project?.descriptor || ""}</div>
      </div>
      <div class="cc-header-controls">
        <span class="cc-freshness ${isWarn ? "warn" : ""}" title="Data as of, not last synced — the stamp only moves when the underlying data changes.">
          Data as of ${formatAbsoluteDate(generatedAt)} (${relativeAge(generatedAt)})${isWarn ? " — sync from the portal to refresh" : ""}
        </span>
        <div class="cc-mode-toggle" role="group" aria-label="Data mode">
          <button data-mode="real" class="${ctx.mode === "real" ? "active" : ""}">Real</button>
          <button data-mode="sample" class="${ctx.mode === "sample" ? "active sample" : ""}">Sample</button>
        </div>
      </div>
    </div>
    <nav class="cc-nav">
      ${TABS.map((t) => `<a href="#/${t.key}" data-key="${t.key}">${t.label}</a>`).join("")}
    </nav>
  `;
  header.querySelectorAll(".cc-mode-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setMode(btn.dataset.mode);
      renderApp();
    });
  });
  root.appendChild(header);
}

async function renderApp() {
  const appRoot = document.getElementById("cc-app");
  appRoot.innerHTML = "";

  let data;
  try {
    data = await loadData();
  } catch (err) {
    appRoot.innerHTML = `<div class="cc-main"><div class="cc-empty"><strong>Could not load project data.</strong>${err.message}</div></div>`;
    return;
  }

  const ctx = { data, mode: getMode(), tabs: TABS, navigate: (hash) => { window.location.hash = hash; } };
  const shell = document.createElement("div");
  shell.className = "cc-app";
  renderHeader(shell, ctx);

  const main = document.createElement("main");
  main.className = "cc-main";
  shell.appendChild(main);

  const footer = document.createElement("footer");
  footer.className = "cc-footer";
  footer.textContent = "SupplyMind AI — Command Center";
  shell.appendChild(footer);

  appRoot.appendChild(shell);

  const { key, rest } = currentRoute();
  shell.querySelectorAll(".cc-nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.key === key);
  });

  const tabDef = TABS.find((t) => t.key === key) || TABS[0];
  if (tabDef.key === "overview") {
    renderOverview(main, ctx);
  } else {
    renderStub(main, ctx, tabDef, rest);
  }
}

window.addEventListener("hashchange", renderApp);
window.addEventListener("DOMContentLoaded", renderApp);
