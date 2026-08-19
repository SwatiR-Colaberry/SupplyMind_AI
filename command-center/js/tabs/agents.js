import { escapeHtml } from "../format.js";
import { storyById } from "../data.js";
import { emptyState } from "../ui.js";
import { SAMPLE_AGENT_RUNS } from "../sampleData.js";

export function renderAgents(main, ctx) {
  const agents = ctx.data.plan?.agents || [];
  const orchestrator = storyById(ctx.data.stories, "STORY-002");
  const mode = ctx.mode;

  main.innerHTML = `
    <h1 class="cc-page-title">AI Agents</h1>
    <p class="cc-page-sub">The agent roster and what each one owns.</p>
    ${
      agents.length
        ? `<div class="cc-card-grid">${agents.map((a) => `<div class="cc-card"><div class="cc-stat-label">Agent</div><div>${escapeHtml(a.name || a.id)}</div></div>`).join("")}</div>`
        : emptyState(
            "No AI agents registered yet",
            `plan.json's agents list is empty — nothing has been orchestrated yet.${
              orchestrator
                ? ` REQ-002 and REQ-004 call for an Orchestrator Agent to coordinate and validate agent responses, tracked as <a href="#/pm/story/STORY-002">${escapeHtml(orchestrator.id)}</a> (${escapeHtml(orchestrator.verification?.state || "not_started").replace("_", " ")}).`
                : ""
            }`
          )
    }
    ${
      mode === "sample"
        ? `
      <div class="cc-section">
        <h2>Agent run history <span class="cc-sample-flag">Sample</span></h2>
        <div class="cc-card-grid">
          <div class="cc-card"><div class="cc-stat-label">Runs recorded</div><div class="cc-stat-value">${SAMPLE_AGENT_RUNS.runs_recorded}</div></div>
          <div class="cc-card"><div class="cc-stat-label">Success rate</div><div class="cc-stat-value">${Math.round(SAMPLE_AGENT_RUNS.success_rate * 100)}%</div></div>
          <div class="cc-card"><div class="cc-stat-label">Last run</div><div class="cc-stat-value" style="font-size:16px;">${new Date(SAMPLE_AGENT_RUNS.last_run).toLocaleString()}</div></div>
        </div>
        <p class="cc-stat-sub">This is what agent telemetry will look like once agents exist — fabricated for preview, never merged with the real (currently empty) roster above.</p>
      </div>`
        : ""
    }
  `;
}
