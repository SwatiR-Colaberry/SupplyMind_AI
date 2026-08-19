import { emptyState } from "../ui.js";

export function renderGuardrails(main, ctx) {
  const guardrails = ctx.data.plan?.derived?.guardrails || [];

  main.innerHTML = `
    <h1 class="cc-page-title">Guardrails</h1>
    <p class="cc-page-sub">What must never happen.</p>
    ${
      guardrails.length
        ? `<div class="cc-card-grid">${guardrails
            .map((g) => `<div class="cc-stat-tile"><div class="cc-stat-label">Guardrail</div><div>${g.statement || g.name || JSON.stringify(g)}</div></div>`)
            .join("")}</div>`
        : emptyState(
            "No guardrails defined yet",
            "plan.json's derived.guardrails list is empty. Guardrails will appear here as they're added to the project plan — nothing is fabricated to fill this space."
          )
    }
  `;
}
