import { escapeHtml } from "../format.js";
import { requirementsFulfilledBy, storyById } from "../data.js";
import { statusBadge, cardLink, breadcrumb, emptyState } from "../ui.js";

function storiesTable(stories, plan) {
  if (!stories.length) return emptyState("No stories", "No stories in this release yet.");
  const rows = stories
    .map((s) => {
      const release = (plan?.releases || []).find((r) => (r.story_ids || []).includes(s.id));
      return `
        <tr>
          <td><a href="#/pm/story/${encodeURIComponent(s.id)}">${escapeHtml(s.id)}</a></td>
          <td><a href="#/pm/story/${encodeURIComponent(s.id)}">${escapeHtml(s.title)}</a></td>
          <td>${escapeHtml(release?.key || "—")}</td>
          <td>${escapeHtml(s.due_on || "—")}</td>
          <td>${statusBadge(s.verification?.state)}</td>
          <td>${s.verification?.points ?? 0}</td>
        </tr>`;
    })
    .join("");
  return `
    <div class="cc-table-wrap">
      <table class="cc-table">
        <thead><tr><th>ID</th><th>Title</th><th>Release</th><th>Due</th><th>Status</th><th>Points</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderReleaseList(main, ctx) {
  const { plan } = ctx.data;
  const stories = ctx.data.stories;
  const releases = plan?.releases || [];

  const cards = releases
    .map((r) => {
      const inRelease = stories.filter((s) => (r.story_ids || []).includes(s.id));
      const verified = inRelease.filter((s) => s.verification?.state === "verified").length;
      const pct = inRelease.length ? Math.round((verified / inRelease.length) * 100) : 0;
      return cardLink(
        `#/pm/release/${encodeURIComponent(r.key)}`,
        `
          <div class="cc-stat-label">${escapeHtml(r.key)}${r.is_demo_target ? ' <span class="cc-badge status-warning">Demo target</span>' : ""}</div>
          <div class="cc-stat-value" style="font-size:18px;">${escapeHtml(r.name)}</div>
          <div class="cc-stat-sub">${escapeHtml(r.starts_on)} → ${escapeHtml(r.ends_on)}</div>
          <div class="cc-progress-track" style="margin-top:8px;"><div class="cc-progress-fill" style="width:${pct}%;"></div></div>
          <div class="cc-stat-sub">${verified} of ${inRelease.length} stories verified</div>
        `
      );
    })
    .join("");

  main.innerHTML = `
    <h1 class="cc-page-title">Project Management</h1>
    <p class="cc-page-sub">Releases, schedule, and every task's status — read from .colaberry/plan.json and .colaberry/progress.json.</p>
    <div class="cc-section">
      <h2>Releases</h2>
      <div class="cc-card-grid">${cards}</div>
    </div>
    <div class="cc-section">
      <h2>All stories (${stories.length})</h2>
      ${storiesTable(stories, plan)}
    </div>
  `;
}

function renderReleaseDetail(main, ctx, key) {
  const { plan } = ctx.data;
  const stories = ctx.data.stories;
  const release = (plan?.releases || []).find((r) => r.key === key);

  if (!release) {
    main.innerHTML = `${breadcrumb([{ label: "Project Management", href: "#/pm" }, { label: key }])}
      ${emptyState("Release not found", `No release with key "${escapeHtml(key)}" in plan.json.`)}`;
    return;
  }

  const inRelease = stories.filter((s) => (release.story_ids || []).includes(s.id));

  main.innerHTML = `
    ${breadcrumb([{ label: "Project Management", href: "#/pm" }, { label: release.key }])}
    <h1 class="cc-page-title">${escapeHtml(release.key)} — ${escapeHtml(release.name)}${release.is_demo_target ? ' <span class="cc-badge status-warning">Demo target</span>' : ""}</h1>
    <p class="cc-page-sub">${escapeHtml(release.starts_on)} → ${escapeHtml(release.ends_on)}</p>
    <div class="cc-section">
      <h2>Stories in this release</h2>
      ${storiesTable(inRelease, plan)}
    </div>
  `;
}

function renderStoryDetail(main, ctx, storyId) {
  const { plan } = ctx.data;
  const stories = ctx.data.stories;
  const story = storyById(stories, storyId);

  if (!story) {
    main.innerHTML = `${breadcrumb([{ label: "Project Management", href: "#/pm" }, { label: storyId }])}
      ${emptyState("Story not found", `No story with id "${escapeHtml(storyId)}" in plan.json or progress.json.`)}`;
    return;
  }

  const release = (plan?.releases || []).find((r) => (r.story_ids || []).includes(story.id));
  const reqs = requirementsFulfilledBy(plan, story.id);
  const criteria = story.criteria || [];

  const criteriaHtml = criteria.length
    ? `<ul class="cc-list">${criteria.map((c) => `<li>${c.passed ? "✅" : "⬜"} ${escapeHtml(c.text)}</li>`).join("")}</ul>`
    : `<p class="cc-stat-sub">Acceptance criteria not written yet — they get written when this story is picked up.</p>`;

  const reqsHtml = reqs.length
    ? `<ul class="cc-list">${reqs.map((r) => `<li>${escapeHtml(r.id)} — ${escapeHtml(r.statement)}</li>`).join("")}</ul>`
    : `<p class="cc-stat-sub">No requirements in plan.json reference this story yet.</p>`;

  main.innerHTML = `
    ${breadcrumb([
      { label: "Project Management", href: "#/pm" },
      ...(release ? [{ label: release.key, href: `#/pm/release/${encodeURIComponent(release.key)}` }] : []),
      { label: story.id },
    ])}
    <h1 class="cc-page-title">${escapeHtml(story.id)} — ${escapeHtml(story.title)}</h1>
    <p class="cc-page-sub">${escapeHtml(story.narrative || "")}</p>
    <div class="cc-card-grid">
      <div class="cc-stat-tile">
        <div class="cc-stat-label">Status</div>
        <div style="margin-top:4px;">${statusBadge(story.verification?.state)}</div>
      </div>
      <div class="cc-stat-tile">
        <div class="cc-stat-label">Release</div>
        <div class="cc-stat-value" style="font-size:18px;">${escapeHtml(release?.key || "pre-work")}</div>
      </div>
      <div class="cc-stat-tile">
        <div class="cc-stat-label">Due</div>
        <div class="cc-stat-value" style="font-size:18px;">${escapeHtml(story.due_on || "—")}</div>
      </div>
      <div class="cc-stat-tile">
        <div class="cc-stat-label">Points</div>
        <div class="cc-stat-value" style="font-size:18px;">${story.verification?.points ?? 0}</div>
      </div>
    </div>
    <div class="cc-section">
      <h2>Acceptance criteria</h2>
      ${criteriaHtml}
    </div>
    <div class="cc-section">
      <h2>Requirements this fulfills</h2>
      ${reqsHtml}
    </div>
  `;
}

export function renderPM(main, ctx, rest) {
  if (rest?.[0] === "release" && rest[1]) return renderReleaseDetail(main, ctx, rest[1]);
  if (rest?.[0] === "story" && rest[1]) return renderStoryDetail(main, ctx, rest[1]);
  renderReleaseList(main, ctx);
}
