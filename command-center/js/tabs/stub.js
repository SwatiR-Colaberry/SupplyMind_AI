export function renderStub(main, ctx, tabDef) {
  main.innerHTML = `
    <h1 class="cc-page-title">${tabDef.title}</h1>
    <p class="cc-page-sub">${tabDef.blurb}</p>
    <div class="cc-empty">
      <strong>Not built yet</strong>
      This tab is reachable from the nav above, but hasn't been built. Say
      <strong>&nbsp;"build the rest"&nbsp;</strong> once the Overview tab looks right, and this
      becomes a real page — with drill-downs — pulled from the same
      <code>.colaberry/plan.json</code> and <code>.colaberry/progress.json</code> files as Overview.
    </div>
  `;
}
