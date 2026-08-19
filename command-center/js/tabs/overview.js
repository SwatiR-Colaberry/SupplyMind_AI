import { escapeHtml } from "../format.js";
import { SAMPLE_OVERVIEW_TOTALS } from "../sampleData.js";

function currentReleaseInfo(plan) {
  const schedule = plan?.schedule;
  const releases = plan?.releases || [];
  if (!schedule) return null;

  const today = new Date();
  const toDate = (s) => new Date(s + "T00:00:00Z");
  const buildStart = toDate(schedule.build_start);
  const buildEnd = toDate(schedule.build_end);
  const demoDay = toDate(schedule.demo_day);

  let active = releases.find((r) => today >= toDate(r.starts_on) && today <= toDate(r.ends_on));
  let phase = "in-build";
  if (today < buildStart) phase = "not-started";
  else if (today > buildEnd && today <= demoDay) phase = "demo-prep";
  else if (today > demoDay) phase = "post-demo";

  const totalSpan = buildEnd - buildStart;
  const elapsed = Math.min(Math.max(today - buildStart, 0), totalSpan);
  const pct = totalSpan > 0 ? Math.round((elapsed / totalSpan) * 100) : 0;

  return { active, phase, pct, buildStart, buildEnd, demoDay };
}

function sampleBadge(mode) {
  return mode === "sample" ? `<span class="cc-sample-flag">Sample</span>` : "";
}

export function renderOverview(main, ctx) {
  const { plan, progress } = ctx.data;
  const mode = ctx.mode;
  const totals = mode === "sample" ? SAMPLE_OVERVIEW_TOTALS : progress?.totals;
  const releaseInfo = currentReleaseInfo(plan);

  const phaseLabel = {
    "not-started": "Build has not started yet",
    "in-build": releaseInfo?.active ? `In release <strong>${escapeHtml(releaseInfo.active.key)} — ${escapeHtml(releaseInfo.active.name)}</strong>` : "Between releases",
    "demo-prep": "Build is complete — this is demo-prep week",
    "post-demo": "Past demo day",
  }[releaseInfo?.phase] || "Schedule not available";

  main.innerHTML = `
    <div class="cc-banner">
      <div>
        <strong>Build paused at the Overview checkpoint.</strong>
        This is the first of nine tabs, built and reviewed on its own before the rest.
        The other eight tabs are reachable from the nav above — each shows a plain
        "not built yet" state, nothing is locked or hidden.
      </div>
      <div>Say <strong>"build the rest"</strong> to continue.</div>
    </div>

    <h1 class="cc-page-title">${escapeHtml(plan?.project?.name || "SupplyMind AI")} ${sampleBadge(mode)}</h1>
    <p class="cc-page-sub">${escapeHtml(plan?.project?.descriptor || "")}</p>

    <div class="cc-section">
      <h2>Where we are</h2>
      <div class="cc-card">
        <div>${phaseLabel}</div>
        <div class="cc-progress-track" style="margin-top:8px;">
          <div class="cc-progress-fill" style="width:${releaseInfo ? Math.min(Math.max(releaseInfo.pct, 0), 100) : 0}%;"></div>
        </div>
        <div class="cc-stat-sub" style="margin-top:6px;">
          Build ${plan?.schedule?.build_start} → ${plan?.schedule?.build_end} · Demo day ${plan?.schedule?.demo_day} · Demo target release <strong>${escapeHtml(plan?.schedule?.demo_release_key || "—")}</strong>
        </div>
      </div>
    </div>

    <div class="cc-section">
      <h2>Progress ${sampleBadge(mode)}</h2>
      <div class="cc-card-grid">
        <div class="cc-card">
          <div class="cc-stat-label">Stories verified</div>
          <div class="cc-stat-value">${totals?.stories_verified ?? 0} <span class="cc-stat-sub">of ${totals?.stories_total ?? 0}</span></div>
        </div>
        <div class="cc-card">
          <div class="cc-stat-label">Criteria passed</div>
          <div class="cc-stat-value">${totals?.criteria_passed ?? 0} <span class="cc-stat-sub">of ${totals?.criteria_total ?? 0}</span></div>
        </div>
        <div class="cc-card">
          <div class="cc-stat-label">Points awarded</div>
          <div class="cc-stat-value">${totals?.points_awarded ?? 0}</div>
        </div>
      </div>
      ${mode === "real" && (progress?.totals?.criteria_total ?? 0) <= 5 ? `
        <p class="cc-stat-sub">Only STORY-000 (this Command Center) has acceptance criteria written so far — every other story's criteria get written when that story is picked up. That's why the criteria total is low right now, not a bug.</p>
      ` : ""}
    </div>

    <div class="cc-section">
      <h2>What's live</h2>
      <div class="cc-card">
        <div><span class="cc-status-dot"></span>System connectivity (PostgreSQL, Google Sheets) — not checked from here yet. <a href="#/systems">See Systems tab</a>.</div>
      </div>
    </div>
  `;
}
