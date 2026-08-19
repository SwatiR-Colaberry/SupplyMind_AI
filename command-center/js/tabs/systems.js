import { escapeHtml } from "../format.js";
import { storyById } from "../data.js";
import { statusBadge, cardLink, breadcrumb, emptyState } from "../ui.js";
import { SAMPLE_SYSTEMS } from "../sampleData.js";

function sampleBadge(mode) {
  return mode === "sample" ? `<span class="cc-sample-flag">Sample</span>` : "";
}

function connectivityFor(name, mode) {
  if (mode !== "sample") {
    return { status: "unknown", label: "Not checked from here yet" };
  }
  const sample = SAMPLE_SYSTEMS.find((s) => s.name === name);
  if (!sample) return { status: "unknown", label: "Not checked from here yet" };
  return { status: sample.status, label: `Last checked ${new Date(sample.last_checked).toLocaleString()}` };
}

function renderSystemList(main, ctx) {
  const systems = ctx.data.plan?.derived?.systems || [];
  const mode = ctx.mode;

  if (!systems.length) {
    main.innerHTML = `<h1 class="cc-page-title">Systems</h1>${emptyState("No systems defined yet", "No systems are recorded in plan.json yet.")}`;
    return;
  }

  const cards = systems
    .map((name) => {
      const conn = connectivityFor(name, mode);
      return cardLink(
        `#/systems/system/${encodeURIComponent(name)}`,
        `
          <div class="cc-stat-label">System</div>
          <div class="cc-stat-value" style="font-size:18px;">${escapeHtml(name)}</div>
          <div class="cc-stat-sub"><span class="cc-status-dot ${conn.status}"></span>${escapeHtml(conn.label)} ${sampleBadge(mode)}</div>
        `
      );
    })
    .join("");

  main.innerHTML = `
    <h1 class="cc-page-title">Systems</h1>
    <p class="cc-page-sub">What this connects to.</p>
    <div class="cc-card-grid">${cards}</div>
  `;
}

function renderSystemDetail(main, ctx, name) {
  const systems = ctx.data.plan?.derived?.systems || [];
  const mode = ctx.mode;

  if (!systems.includes(name)) {
    main.innerHTML = `${breadcrumb([{ label: "Systems", href: "#/systems" }, { label: name }])}
      ${emptyState("System not found", `"${escapeHtml(name)}" is not in plan.json's systems list.`)}`;
    return;
  }

  const conn = connectivityFor(name, mode);
  const usedReqs = (ctx.data.plan?.requirements || []).filter((r) => r.statement.toLowerCase().includes(name.toLowerCase()));

  const storyRows = usedReqs
    .flatMap((r) => r.fulfilled_by || [])
    .filter((id, i, arr) => arr.indexOf(id) === i)
    .map((id) => storyById(ctx.data.stories, id))
    .filter(Boolean);

  main.innerHTML = `
    ${breadcrumb([{ label: "Systems", href: "#/systems" }, { label: name }])}
    <h1 class="cc-page-title">${escapeHtml(name)}</h1>
    <div class="cc-stat-tile">
      <div class="cc-stat-label">Connectivity ${sampleBadge(mode)}</div>
      <div style="margin-top:4px;"><span class="cc-status-dot ${conn.status}"></span>${escapeHtml(conn.label)}</div>
    </div>
    <div class="cc-section">
      <h2>Requirements involving this system</h2>
      ${
        usedReqs.length
          ? `<ul class="cc-list">${usedReqs.map((r) => `<li>${escapeHtml(r.id)} — ${escapeHtml(r.statement)}</li>`).join("")}</ul>`
          : emptyState("None yet", "No requirement in plan.json names this system.")
      }
    </div>
    <div class="cc-section">
      <h2>Stories that touch it</h2>
      ${
        storyRows.length
          ? `<div class="cc-table-wrap"><table class="cc-table">
              <thead><tr><th>ID</th><th>Title</th><th>Status</th></tr></thead>
              <tbody>${storyRows
                .map((s) => `<tr><td><a href="#/pm/story/${encodeURIComponent(s.id)}">${escapeHtml(s.id)}</a></td><td>${escapeHtml(s.title)}</td><td>${statusBadge(s.verification?.state)}</td></tr>`)
                .join("")}</tbody>
            </table></div>`
          : emptyState("None yet", "No story is linked to this system through plan.json yet.")
      }
    </div>
  `;
}

export function renderSystems(main, ctx, rest) {
  if (rest?.[0] === "system" && rest[1]) return renderSystemDetail(main, ctx, rest[1]);
  renderSystemList(main, ctx);
}
