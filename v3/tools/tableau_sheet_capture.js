// Tableau per-sheet reference capture (schema tableau-sheet-capture/1).
//
// Run in a browser page on the Tableau site's own origin while signed in (or, in the planned
// worker, in a Playwright page with a Connected App JWT set on the viz `token`). It loads the
// dashboard with the official Embedding API v3 and reads every worksheet's summary data —
// the values Tableau itself computed — including sheets that exist only inside the dashboard.
//
// Usage: await captureDashboard('https://<pod>.online.tableau.com', '/t/<site>/views/<Workbook>/<View>')
//        await captureStates(origin, viewPath, plan)   (plan = the app's 'filter_states_plan.json'; SL06)
//        await captureActions(origin, viewPath, plan)  (plan = the app's 'actions_plan.json'; AC02)
// Returns a JSON-serialisable capture; nothing is written anywhere by this script.
async function captureDashboard(origin, viewPath, { maxRows = 5000, token = null } = {}) {
  const api = await import(origin + '/javascripts/api/tableau.embedding.3.latest.min.js');
  const holder = document.createElement('div');
  holder.style.cssText = 'position:fixed;left:0;top:0;width:1200px;height:900px;z-index:99999;background:#fff';
  document.body.appendChild(holder);
  const viz = new api.TableauViz();
  viz.src = origin + viewPath;
  viz.toolbar = 'hidden';
  if (token) viz.token = token;
  holder.appendChild(viz);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Tableau view did not become interactive')), 90000);
    viz.addEventListener(api.TableauEventType.FirstInteractive, () => { clearTimeout(timer); resolve(); });
  });
  const active = viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'dashboard' ? active.worksheets : [active];
  const capture = { schema: 'tableau-sheet-capture/1',
    method: 'Tableau Embedding API v3 Worksheet.getSummaryDataReaderAsync',
    view: viewPath, dashboard: active.name, captured_at: new Date().toISOString(), sheets: {} };
  for (const ws of worksheets) {
    try {
      const reader = await ws.getSummaryDataReaderAsync();
      const table = await reader.getAllPagesAsync();
      await reader.releaseAsync();
      const total = table.totalRowCount ?? table.data.length;
      capture.sheets[ws.name] = {
        columns: table.columns.map(c => c.fieldName),
        rows: table.data.slice(0, maxRows).map(r => r.map(c => c.formattedValue)),
        total_rows: total,
        note: total > maxRows ? `first ${maxRows} of ${total} rows` : null };
    } catch (e) {
      capture.sheets[ws.name] = { error: String(e).slice(0, 200) };
    }
  }
  holder.remove();
  return capture;
}

// SL06: the same capture in several quick-filter states (schema tableau-sheet-capture-states/1).
// `plan` is the JSON the app writes ("Plan the filter states"): {dashboard, states: [{id, field, values}]}.
// For each state the filter is applied (replace) on every worksheet that carries it, every sheet's summary data is
// read, and that worksheet's original selection is restored before the next state. Nothing is written anywhere.
async function captureStates(origin, viewPath, plan, { maxRows = 5000, token = null } = {}) {
  const api = await import(origin + '/javascripts/api/tableau.embedding.3.latest.min.js');
  const holder = document.createElement('div');
  holder.style.cssText = 'position:fixed;left:0;top:0;width:1200px;height:900px;z-index:99999;background:#fff';
  document.body.appendChild(holder);
  const viz = new api.TableauViz();
  viz.src = origin + viewPath;
  viz.toolbar = 'hidden';
  if (token) viz.token = token;
  holder.appendChild(viz);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Tableau view did not become interactive')), 90000);
    viz.addEventListener(api.TableauEventType.FirstInteractive, () => { clearTimeout(timer); resolve(); });
  });
  const active = viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'dashboard' ? active.worksheets : [active];
  const read = async () => {
    const sheets = {};
    for (const ws of worksheets) {
      try {
        const reader = await ws.getSummaryDataReaderAsync();
        const table = await reader.getAllPagesAsync();
        await reader.releaseAsync();
        const total = table.totalRowCount ?? table.data.length;
        sheets[ws.name] = { columns: table.columns.map(c => c.fieldName),
          rows: table.data.slice(0, maxRows).map(r => r.map(c => c.formattedValue)), total_rows: total,
          note: total > maxRows ? `first ${maxRows} of ${total} rows` : null };
      } catch (e) {
        sheets[ws.name] = { error: String(e).slice(0, 200) };
      }
    }
    return sheets;
  };
  const one = (sheets) => ({ schema: 'tableau-sheet-capture/1', view: viewPath, dashboard: active.name,
    captured_at: new Date().toISOString(), sheets });
  const bundle = { schema: 'tableau-sheet-capture-states/1',
    method: 'Tableau Embedding API v3 applyFilterAsync + Worksheet.getSummaryDataReaderAsync',
    view: viewPath, dashboard: active.name, captured_at: new Date().toISOString(),
    default: one(await read()), states: [] };
  for (const state of (plan && plan.states) || []) {
    const touched = [];
    try {
      // record every original selection first: a shared quick filter changes all its worksheets at once
      for (const ws of worksheets) {
        const filters = await ws.getFiltersAsync();
        const f = filters.find(x => x.fieldName === state.field);
        if (!f) continue;
        touched.push({ ws, prior: f.isAllSelected ? null : (f.appliedValues || []).map(v => v.formattedValue) });
      }
      for (const t of touched) await t.ws.applyFilterAsync(state.field, state.values, 'replace');
      bundle.states.push({ id: state.id, field: state.field, values: state.values,
        applied_on: touched.map(t => t.ws.name), capture: one(touched.length ? await read() : {}) });
    } catch (e) {
      bundle.states.push({ id: state.id, field: state.field, values: state.values, applied_on: [],
        error: String(e).slice(0, 200), capture: one({}) });
    } finally {
      for (const t of touched.slice().reverse()) {           // restore each worksheet's own selection
        try {
          if (t.prior === null) await t.ws.clearFilterAsync(state.field);
          else await t.ws.applyFilterAsync(state.field, t.prior, 'replace');
        } catch (e) { /* the next state still replaces the filter */ }
      }
    }
  }
  holder.remove();
  return bundle;
}

// AC02: dashboard actions (schema tableau-sheet-capture-actions/1). `plan` is the app's "actions_plan.json":
// {dashboard, actions: [{action_id, source, tableau_field, tableau_values, values}]}. For each action the marks are
// selected on the source sheet (which fires Tableau's own filter action), every sheet is read, and the selection is
// cleared; after the last action every sheet is read once more (Reset). Nothing is written anywhere.
async function captureActions(origin, viewPath, plan, { maxRows = 5000, token = null, settleMs = 1500 } = {}) {
  const api = await import(origin + '/javascripts/api/tableau.embedding.3.latest.min.js');
  const holder = document.createElement('div');
  holder.style.cssText = 'position:fixed;left:0;top:0;width:1200px;height:900px;z-index:99999;background:#fff';
  document.body.appendChild(holder);
  const viz = new api.TableauViz();
  viz.src = origin + viewPath;
  viz.toolbar = 'hidden';
  if (token) viz.token = token;
  holder.appendChild(viz);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Tableau view did not become interactive')), 90000);
    viz.addEventListener(api.TableauEventType.FirstInteractive, () => { clearTimeout(timer); resolve(); });
  });
  const active = viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'dashboard' ? active.worksheets : [active];
  const settle = () => new Promise(r => setTimeout(r, settleMs));
  const read = async () => {
    const sheets = {};
    for (const ws of worksheets) {
      try {
        const reader = await ws.getSummaryDataReaderAsync();
        const table = await reader.getAllPagesAsync();
        await reader.releaseAsync();
        const total = table.totalRowCount ?? table.data.length;
        sheets[ws.name] = { columns: table.columns.map(c => c.fieldName),
          rows: table.data.slice(0, maxRows).map(r => r.map(c => c.formattedValue)), total_rows: total,
          note: total > maxRows ? `first ${maxRows} of ${total} rows` : null };
      } catch (e) {
        sheets[ws.name] = { error: String(e).slice(0, 200) };
      }
    }
    return sheets;
  };
  const one = (sheets) => ({ schema: 'tableau-sheet-capture/1', view: viewPath, dashboard: active.name,
    captured_at: new Date().toISOString(), sheets });
  const bundle = { schema: 'tableau-sheet-capture-actions/1',
    method: 'Tableau Embedding API v3 selectMarksByValueAsync + Worksheet.getSummaryDataReaderAsync',
    view: viewPath, dashboard: active.name, captured_at: new Date().toISOString(),
    default: one(await read()), actions: [], reset: null };
  for (const a of (plan && plan.actions) || []) {
    const ws = worksheets.find(w => w.name === a.source);
    if (!ws) { bundle.actions.push({ action_id: a.action_id, values: a.values, error: 'source sheet not found', capture: one({}) }); continue; }
    try {
      await ws.selectMarksByValueAsync([{ fieldName: a.tableau_field, value: a.tableau_values }], 'select-replace');
      await settle();
      bundle.actions.push({ action_id: a.action_id, values: a.values, tableau_values: a.tableau_values,
        capture: one(await read()) });
    } catch (e) {
      bundle.actions.push({ action_id: a.action_id, values: a.values, error: String(e).slice(0, 200), capture: one({}) });
    } finally {
      try { await ws.clearSelectedMarksAsync(); await settle(); } catch (e) { /* next action replaces the selection */ }
    }
  }
  bundle.reset = one(await read());          // every selection cleared: Tableau's state after Reset
  holder.remove();
  return bundle;
}
