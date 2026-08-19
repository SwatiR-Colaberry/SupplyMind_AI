import { escapeHtml } from "../format.js";
import { storyById } from "../data.js";
import { statusBadge, breadcrumb, emptyState } from "../ui.js";

function renderReqList(main, ctx) {
  const reqs = ctx.data.plan?.requirements || [];

  const rows = reqs
    .map(
      (r) => `
      <tr>
        <td><a href="#/kb/req/${encodeURIComponent(r.id)}">${escapeHtml(r.id)}</a></td>
        <td>${escapeHtml(r.kind)}</td>
        <td>${escapeHtml(r.priority)}</td>
        <td>${escapeHtml(r.cluster)}</td>
        <td>${escapeHtml(r.statement)}</td>
        <td>${(r.fulfilled_by || []).join(", ") || "—"}</td>
      </tr>`
    )
    .join("");

  main.innerHTML = `
    <h1 class="cc-page-title">Knowledge Base</h1>
    <p class="cc-page-sub">Requirements, stories, traceability, and a chat panel over this project's own data.</p>
    <div class="cc-section">
      <h2>Requirements (${reqs.length})</h2>
      <div class="cc-table-wrap">
        <table class="cc-table">
          <thead><tr><th>ID</th><th>Kind</th><th>Priority</th><th>Cluster</th><th>Statement</th><th>Fulfilled by</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
    <div class="cc-section">
      <h2>Chat over this project's data</h2>
      ${emptyState(
        "Not built yet",
        "A chat panel here needs a real backend grounded in plan.json and progress.json to answer honestly — wiring up a chat box without one would mean fabricating answers, which breaks the Trust rule this whole page is built around. It'll ship once that backend exists."
      )}
    </div>
  `;
}

function renderReqDetail(main, ctx, reqId) {
  const reqs = ctx.data.plan?.requirements || [];
  const req = reqs.find((r) => r.id === reqId);

  if (!req) {
    main.innerHTML = `${breadcrumb([{ label: "Knowledge Base", href: "#/kb" }, { label: reqId }])}
      ${emptyState("Requirement not found", `No requirement with id "${escapeHtml(reqId)}" in plan.json.`)}`;
    return;
  }

  const stories = (req.fulfilled_by || []).map((id) => storyById(ctx.data.stories, id)).filter(Boolean);

  main.innerHTML = `
    ${breadcrumb([{ label: "Knowledge Base", href: "#/kb" }, { label: req.id }])}
    <h1 class="cc-page-title">${escapeHtml(req.id)}</h1>
    <p class="cc-page-sub">${escapeHtml(req.statement)}</p>
    <div class="cc-card-grid">
      <div class="cc-stat-tile"><div class="cc-stat-label">Kind</div><div>${escapeHtml(req.kind)}</div></div>
      <div class="cc-stat-tile"><div class="cc-stat-label">Priority</div><div>${escapeHtml(req.priority)}</div></div>
      <div class="cc-stat-tile"><div class="cc-stat-label">Cluster</div><div>${escapeHtml(req.cluster)}</div></div>
    </div>
    <div class="cc-section">
      <h2>Stories fulfilling this requirement</h2>
      ${
        stories.length
          ? `<div class="cc-table-wrap"><table class="cc-table">
              <thead><tr><th>ID</th><th>Title</th><th>Status</th></tr></thead>
              <tbody>${stories
                .map((s) => `<tr><td><a href="#/pm/story/${encodeURIComponent(s.id)}">${escapeHtml(s.id)}</a></td><td>${escapeHtml(s.title)}</td><td>${statusBadge(s.verification?.state)}</td></tr>`)
                .join("")}</tbody>
            </table></div>`
          : emptyState("None yet", "No story in plan.json is marked as fulfilling this requirement.")
      }
    </div>
  `;
}

export function renderKB(main, ctx, rest) {
  if (rest?.[0] === "req" && rest[1]) return renderReqDetail(main, ctx, rest[1]);
  renderReqList(main, ctx);
}
