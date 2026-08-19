import { emptyState } from "../ui.js";

export function renderOutcomes(main, ctx) {
  const measures = ctx.data.plan?.derived?.measures || [];

  main.innerHTML = `
    <h1 class="cc-page-title">Outcomes</h1>
    <p class="cc-page-sub">The numbers this project is meant to move.</p>
    ${
      measures.length
        ? `<div class="cc-card-grid">${measures
            .map((m) => `<div class="cc-stat-tile"><div class="cc-stat-label">${m.name || m.id}</div><div class="cc-stat-value">${m.value ?? "—"}</div></div>`)
            .join("")}</div>`
        : emptyState(
            "No outcome measures defined yet",
            "plan.json's derived.measures list is empty — no baseline or target metrics have been set for this project yet. This page will show them, sourced from that file, once they exist."
          )
    }
  `;
}
