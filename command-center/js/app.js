import { loadData } from "./data.js";
import { formatAbsoluteDate, relativeAge, ageInDays } from "./format.js";
import { renderOverview } from "./tabs/overview.js";
import { renderOutcomes } from "./tabs/outcomes.js";
import { renderUsers } from "./tabs/users.js";
import { renderGuardrails } from "./tabs/guardrails.js";
import { renderSystems } from "./tabs/systems.js";
import { renderPM } from "./tabs/pm.js";
import { renderAgents } from "./tabs/agents.js";
import { renderKB } from "./tabs/kb.js";
import { renderDataModel } from "./tabs/datamodel.js";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "outcomes", label: "Outcomes" },
  { key: "users", label: "Users & Use Case" },
  { key: "guardrails", label: "Guardrails" },
  { key: "systems", label: "Systems" },
  { key: "pm", label: "Project Management" },
  { key: "agents", label: "AI Agents" },
  { key: "kb", label: "Knowledge Base" },
  { key: "data-model", label: "Data Model" },
];

const RENDERERS = {
  overview: renderOverview,
  outcomes: renderOutcomes,
  users: renderUsers,
  guardrails: renderGuardrails,
  systems: renderSystems,
  pm: renderPM,
  agents: renderAgents,
  kb: renderKB,
  "data-model": renderDataModel,
};

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
  const renderer = RENDERERS[tabDef.key] || renderOverview;
  renderer(main, ctx, rest);
}

window.addEventListener("hashchange", renderApp);
window.addEventListener("DOMContentLoaded", renderApp);
