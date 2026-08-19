import { escapeHtml } from "../format.js";
import { personaFromNarrative } from "../data.js";
import { statusBadge, cardLink, breadcrumb, emptyState } from "../ui.js";

function storiesForRole(stories, roleName) {
  return stories.filter((s) => {
    const persona = personaFromNarrative(s.narrative);
    return persona && persona.toLowerCase() === roleName.toLowerCase();
  });
}

function renderRoleList(main, ctx) {
  const roles = ctx.data.plan?.derived?.roles || [];
  const stories = ctx.data.stories;

  if (!roles.length) {
    main.innerHTML = `<h1 class="cc-page-title">Users and Use Case</h1>
      ${emptyState("No roles defined yet", "No user roles are recorded in plan.json yet.")}`;
    return;
  }

  const cards = roles
    .map((r) => {
      const matched = storiesForRole(stories, r.name);
      return cardLink(
        `#/users/role/${encodeURIComponent(r.id)}`,
        `
          <div class="cc-stat-label">Role</div>
          <div class="cc-stat-value" style="font-size:18px;">${escapeHtml(r.name)}</div>
          <div class="cc-stat-sub">${matched.length} stor${matched.length === 1 ? "y" : "ies"} written for this persona</div>
        `
      );
    })
    .join("");

  main.innerHTML = `
    <h1 class="cc-page-title">Users and Use Case</h1>
    <p class="cc-page-sub">Who this is for and what they're trying to get done — derived from the "As a ..." persona in each story's narrative.</p>
    <div class="cc-card-grid">${cards}</div>
  `;
}

function renderRoleDetail(main, ctx, roleId) {
  const roles = ctx.data.plan?.derived?.roles || [];
  const role = roles.find((r) => r.id === roleId);
  const stories = ctx.data.stories;

  if (!role) {
    main.innerHTML = `${breadcrumb([{ label: "Users and Use Case", href: "#/users" }, { label: roleId }])}
      ${emptyState("Role not found", `No role with id "${escapeHtml(roleId)}" in plan.json.`)}`;
    return;
  }

  const matched = storiesForRole(stories, role.name);
  const rows = matched.length
    ? matched
        .map(
          (s) => `
        <tr>
          <td><a href="#/pm/story/${encodeURIComponent(s.id)}">${escapeHtml(s.id)}</a></td>
          <td>${escapeHtml(s.title)}</td>
          <td>${escapeHtml(s.narrative || "")}</td>
          <td>${statusBadge(s.verification?.state)}</td>
        </tr>`
        )
        .join("")
    : "";

  main.innerHTML = `
    ${breadcrumb([{ label: "Users and Use Case", href: "#/users" }, { label: role.name }])}
    <h1 class="cc-page-title">${escapeHtml(role.name)}</h1>
    <p class="cc-page-sub">Stories written from this persona's point of view.</p>
    ${
      matched.length
        ? `<div class="cc-table-wrap"><table class="cc-table">
            <thead><tr><th>ID</th><th>Title</th><th>Narrative</th><th>Status</th></tr></thead>
            <tbody>${rows}</tbody>
          </table></div>`
        : emptyState("No stories yet", "No story narrative currently starts with this persona.")
    }
  `;
}

export function renderUsers(main, ctx, rest) {
  if (rest?.[0] === "role" && rest[1]) return renderRoleDetail(main, ctx, rest[1]);
  renderRoleList(main, ctx);
}
