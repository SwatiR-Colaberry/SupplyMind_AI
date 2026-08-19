const STATE_LABELS = {
  verified: { cls: "status-good", label: "Verified" },
  in_progress: { cls: "status-warning", label: "In progress" },
  not_started: { cls: "status-unknown", label: "Not started" },
};

export function statusBadge(state) {
  const s = STATE_LABELS[state] || STATE_LABELS.not_started;
  return `<span class="cc-badge ${s.cls}">${s.label}</span>`;
}

export function cardLink(href, innerHtml, extraClass = "") {
  return `<a class="cc-card clickable ${extraClass}" href="${href}">${innerHtml}</a>`;
}

export function breadcrumb(items) {
  // items: [{ label, href? }] — last item has no href (current page)
  return `<div class="cc-breadcrumb">${items
    .map((it) => (it.href ? `<a href="${it.href}">${it.label}</a>` : `<span>${it.label}</span>`))
    .join(" / ")}</div>`;
}

export function emptyState(title, body) {
  return `<div class="cc-empty"><strong>${title}</strong>${body}</div>`;
}
