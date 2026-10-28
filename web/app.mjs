export const SAMPLES = [
  {id: 'club_membership_fees', title: 'Club membership', detail: 'Fees, waivers & a missing row', sheets: [{name: 'Members', max_row: 32, max_column: 4}, {name: 'Fee Rules', max_row: 4, max_column: 2}]},
  {id: 'small_business_budget', title: 'Small business', detail: 'Budget formulas & assumptions', sheets: [{name: 'Budget', max_row: 11, max_column: 10}]},
  {id: 'event_budget', title: 'Event budget', detail: 'An empty reference & an override', sheets: [{name: 'Event', max_row: 6, max_column: 5}]},
];

const VERDICTS = {
  error: {label: 'Error', mark: '!', css: 'error'},
  intended_exception: {label: 'Documented exception', mark: '✓', css: 'exception'},
  ask: {label: 'Ask', mark: '?', css: 'ask'},
  assumption_to_verify: {label: 'Assumption to verify', mark: '≈', css: 'assumption'},
  unreviewed: {label: 'Not reviewed', mark: '·', css: 'unreviewed'},
};
const verdict = finding => VERDICTS[finding.verdict] || VERDICTS.unreviewed;
const text = value => value === null || value === undefined ? 'Unknown' : String(value);
const money = value => `$${Number(value || 0).toFixed(5)}`;
const modelName = value => value?.toLowerCase().includes('super') ? 'Nemotron Super' : value === 'fallback' ? 'No model decision' : value || 'Not reviewed';

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function button(label, action, className = '') {
  const node = el('button', className, label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function field(list, label, value, code = false) {
  list.append(el('dt', '', label));
  const dd = el('dd');
  dd.append(el(code ? 'code' : 'span', '', text(value)));
  list.append(dd);
}
function findingAnchor(id) { return `finding-${encodeURIComponent(id)}`; }

export function orderedFindings(report) {
  const byId = new Map(report.findings.map(f => [f.candidate_id || f.id, f]));
  const ids = [...new Set([...(report.summary?.order || []), ...byId.keys()])];
  return ids.filter(id => byId.has(id)).map(id => byId.get(id));
}

function renderFinding(finding, index) {
  const v = verdict(finding);
  const card = el('article', `finding ${v.css}`);
  card.dataset.verdict = finding.verdict;
  card.id = findingAnchor(finding.candidate_id || finding.id);
  card.tabIndex = -1;
  const head = el('div', 'finding-head');
  const heading = el('h3', '', `${finding.sheet}!${finding.cell}`);
  const badge = el('span', `badge ${v.css}`, `${v.mark} ${v.label}`);
  head.append(el('span', 'finding-index', String(index + 1).padStart(2, '0')), heading, badge);
  card.append(head, el('p', 'reason', finding.reason || 'This candidate has not been reviewed.'),
    el('p', 'fine', `Decision: ${modelName(finding.decided_by)}`));
  const evidence = el('dl', 'engine-evidence');
  field(evidence, 'Formula', finding.formula || 'No formula (literal or blank)', true);
  field(evidence, 'Majority pattern · R1C1', finding.majority_r1c1 || 'Not available', true);
  field(evidence, 'Saved value', finding.value, true);
  field(evidence, 'Impact · engine only', finding.impact === null ? 'Unknown — not calculated' : finding.impact);
  card.append(evidence, el('p', 'fine', `Engine signals: ${(finding.kind || []).join(', ')}`));
  if (finding.impact !== null && finding.kind?.includes('aggregate_range_gap')) {
    card.append(el('p', 'fine', 'Impact is the signed sum of omitted saved values; it may differ from a recalculated total.'));
  }
  const quotes = el('div', 'quotes');
  for (const item of finding.evidence || []) {
    const quote = el('figure');
    quote.append(el('blockquote', '', item.quote), el('figcaption', '', `${item.cell} · ${finding.grounding === 'passed' ? 'grounded' : 'not verified'}`));
    quotes.append(quote);
  }
  if (quotes.childElementCount) card.append(quotes);
  if (finding.grounding === 'failed' && finding.verdict === 'ask') {
    card.append(el('p', 'grounding-note', 'Grounding failed — downgraded to ask'),
      el('p', 'fine', (finding.grounding_reasons || []).join('; ')));
  }
  if (finding.fix) {
    const fix = el('p', 'fix', `Suggested fix at ${finding.fix.cell}: `);
    fix.append(el('code', '', finding.fix.new_formula));
    card.append(fix);
  }
  if (finding.question) card.append(el('p', 'question', `Ask: ${finding.question}`));
  return card;
}

function columnName(number) {
  let result = '';
  while (number) { number--; result = String.fromCharCode(65 + number % 26) + result; number = Math.floor(number / 26); }
  return result;
}

function renderMap(sheet, findings, host) {
  const section = el('section', 'sheet-map');
  section.dataset.sheet = sheet.name;
  section.append(el('h4', '', sheet.name));
  const rows = sheet.max_row, cols = sheet.max_column;
  const status = el('p', 'fine');
  status.setAttribute('role', 'status');
  const gridHost = el('div', 'grid-host');
  const byCell = new Map(findings.filter(f => f.sheet === sheet.name).map(f => [f.cell, f]));
  let rowStart = 1, colStart = 1;
  function draw() {
    const rowEnd = Math.min(rows, rowStart + 39), colEnd = Math.min(cols, colStart + 11);
    const table = el('table', 'cell-map');
    const caption = el('caption', 'fine', `${sheet.name} · ${columnName(colStart)}${rowStart}:${columnName(colEnd)}${rowEnd} of A1:${columnName(cols)}${rows}`);
    table.append(caption);
    const tr = el('tr');
    tr.append(el('th', '', ''));
    for (let c = colStart; c <= colEnd; c++) {
      const th = el('th', '', columnName(c)); th.scope = 'col'; tr.append(th);
    }
    const thead = el('thead'); thead.append(tr); table.append(thead);
    const tbody = el('tbody');
    for (let r = rowStart; r <= rowEnd; r++) {
      const row = el('tr'), th = el('th', '', String(r)); th.scope = 'row'; row.append(th);
      for (let c = colStart; c <= colEnd; c++) {
        const address = `${columnName(c)}${r}`, f = byCell.get(address), v = f ? verdict(f) : null;
        const cell = el('td');
        const target = button(v?.mark || '·', () => {
          if (f) {
            const card = host.querySelector(`[id="${findingAnchor(f.candidate_id || f.id)}"]`);
            card?.focus(); card?.scrollIntoView({block: 'nearest'});
          } else status.textContent = `${sheet.name}!${address}: no engine finding.`;
        }, v ? `map-cell ${v.css}` : 'map-cell neutral');
        target.setAttribute('aria-label', `${sheet.name}!${address}: ${v?.label || 'no engine finding'}`);
        target.title = target.getAttribute('aria-label');
        cell.append(target); row.append(cell);
      }
      tbody.append(row);
    }
    table.append(tbody); gridHost.replaceChildren(table);
    prevRows.disabled = rowStart === 1; nextRows.disabled = rowEnd === rows;
    prevCols.disabled = colStart === 1; nextCols.disabled = colEnd === cols;
  }
  const nav = el('div', 'map-nav');
  const prevRows = button('↑ Rows', () => { rowStart = Math.max(1, rowStart - 40); draw(); });
  const nextRows = button('↓ Rows', () => { rowStart += 40; draw(); });
  const prevCols = button('← Columns', () => { colStart = Math.max(1, colStart - 12); draw(); });
  const nextCols = button('→ Columns', () => { colStart += 12; draw(); });
  if (rows > 40) nav.append(prevRows, nextRows);
  if (cols > 12) nav.append(prevCols, nextCols);
  draw(); section.append(gridHost, nav, status); return section;
}

// A shipped sample is either a scripted fixture or a recorded live run; neither costs anything to view.
function sourceText(report) {
  if (report.sample && report.source === 'live') return `Recorded live run (${text(report.generated_at).slice(0, 10)}) — NVIDIA Nemotron via Nebius Token Factory. Viewing costs $0; answers vary between runs.`;
  if (report.sample || report.source === 'mock') return 'Sample result — scripted, not a live model run. Cost $0.';
  return `Live session cost: ${money(report.session_cost_usd)}`;
}

export function markdown(report) {
  const safe = value => text(value).replaceAll('\\', '\\\\').replace(/([`*_[\]<>#|])/g, '\\$1');
  const lines = [`# Exception Keeper: ${safe(report.workbook)}`, '', sourceText(report),
  '', safe(report.summary?.headline || 'Review in progress'), safe(report.summary?.question_for_user || '')];
  for (const f of orderedFindings(report)) {
    lines.push('', `## ${safe(f.sheet)}!${f.cell} — ${verdict(f).label}`, safe(f.reason),
      `Decision: ${safe(modelName(f.decided_by))}`, `Formula: ${safe(f.formula)}`,
      `Majority R1C1: ${safe(f.majority_r1c1)}`, `Saved value: ${safe(f.value)}`, `Engine impact: ${safe(f.impact)}`,
      ...(f.evidence || []).map(e => `Evidence (${safe(e.cell)}): ${safe(e.quote)}`));
    if (f.grounding === 'failed' && f.verdict === 'ask') lines.push('Grounding failed — downgraded to ask');
    if (f.fix) lines.push(`Suggested fix (${safe(f.fix.cell)}): ${safe(f.fix.new_formula)}`);
    if (f.question) lines.push(`Ask: ${safe(f.question)}`);
  }
  return lines.join('\n');
}

export function csv(report) {
  const escape = value => {
    let s = value == null ? '' : String(value);
    if (/^[\s]*[=+\-@]/.test(s) || /^[\t\r\n]/.test(s)) s = "'" + s;
    return `"${s.replaceAll('"', '""')}"`;
  };
  const rows = [['sheet', 'cell', 'verdict', 'formula', 'majority_r1c1', 'saved_value', 'engine_impact', 'reason', 'evidence', 'grounding', 'fix_cell', 'fix_formula', 'question', 'model', 'source'],
    ...orderedFindings(report).map(f => [f.sheet, f.cell, verdict(f).label, f.formula, f.majority_r1c1, f.value, f.impact, f.reason,
      (f.evidence || []).map(e => `${e.cell}: ${e.quote}`).join('; '), f.grounding, f.fix?.cell, f.fix?.new_formula, f.question, f.decided_by, report.source])];
  return rows.map(row => row.map(escape).join(',')).join('\r\n');
}

export function renderReport(host, report) {
  const root = el('div', 'report');
  const sample = report.sample || report.source === 'mock';
  const recorded = report.sample && report.source === 'live';
  root.append(el('p', 'eyebrow', report.workbook || 'YOUR WORKBOOK'),
    el('p', 'source-label', recorded ? `recorded live run · ${text(report.generated_at).slice(0, 10)} · answers vary between runs`
      : sample ? 'sample result — scripted, not a live model run' : 'Live review · grounded against your workbook'),
    el('h2', 'report-headline', report.summary?.headline || 'Reviewing the cells that deserve a second look.'),
    el('p', 'top-question', report.summary?.question_for_user || 'No single question was proposed. Review the questions on individual findings.'),
    el('p', 'cost', recorded ? `Viewing cost $0 · the recorded run cost ${money(report.cost?.total_usd ?? 0)}` : sample ? 'Session cost $0 · precomputed sample' : `Session cost ${money(report.session_cost_usd)} · $0.15 maximum`));
  const exportStatus = el('p', 'fine'); exportStatus.setAttribute('role', 'status');
  const actions = el('div', 'report-actions');
  actions.append(button('Copy findings as Markdown', async () => {
    try { await navigator.clipboard.writeText(markdown(report)); exportStatus.textContent = 'Findings copied.'; }
    catch {
      const area = el('textarea'); area.value = markdown(report); area.setAttribute('aria-label', 'Findings as Markdown');
      exportStatus.replaceChildren(el('span', '', 'Copy is unavailable. Select and copy this text:'), area); area.focus(); area.select();
    }
  }), button('Download findings as CSV', () => {
    const blob = new Blob(['\uFEFF', csv(report)], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob), a = el('a');
    a.href = url; a.download = 'exception-keeper-findings.csv'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    exportStatus.textContent = 'CSV prepared. Formula-like text is escaped for spreadsheet safety.';
  }));
  root.append(actions, exportStatus);
  const findings = orderedFindings(report);
  const layout = el('div', 'results-layout'), maps = el('aside', 'maps');
  maps.append(el('h3', '', 'Where to look'), el('p', 'fine', 'A map of the used range. Select a marked cell to read its finding. Large sheets use row and column pages.'));
  const legend = el('ul', 'legend');
  for (const v of Object.values(VERDICTS)) {
    const count = findings.filter(f => verdict(f) === v).length;
    legend.append(el('li', v.css, `${v.mark} ${v.label} · ${count}`));
  }
  maps.append(legend);
  for (const sheet of report.sheets || []) maps.append(renderMap(sheet, findings, host));
  const list = el('section', 'findings');
  list.append(el('h3', 'list-title', `${findings.length} findings · in review order`));
  findings.forEach((f, index) => list.append(renderFinding(f, index)));
  if (!findings.length) list.append(el('p', '', 'No structural candidates found. This does not establish that the workbook is correct.'));
  layout.append(maps, list); root.append(layout); host.replaceChildren(root);
  return {findings_rendered: findings.length,
    verdict_counts: Object.fromEntries([...new Set(findings.map(f => f.verdict))].map(v => [v, findings.filter(f => f.verdict === v).length])),
    cell_map_rendered: !!host.querySelector('.cell-map')};
}

export async function loadSample(sample) {
  const response = await fetch(`./samples/precomputed/${sample.id}.json`);
  if (!response.ok) throw new Error('Sample unavailable. Check the local preview server.');
  const data = await response.json();
  return {...data, source: data.source === 'live' ? 'live' : 'mock', sample: true, session_cost_usd: 0, sheets: sample.sheets};
}

async function workbookBase64(file) {
  const data = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (const byte of data) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function mountApp() {
  const host = document.querySelector('#results'), progress = document.querySelector('#progress');
  const form = document.querySelector('#review-form'), errorBox = document.querySelector('#form-error');
  const fileInput = document.querySelector('#workbook'), passInput = document.querySelector('#passphrase');
  const runButton = document.querySelector('#run');
  let busy = false, sampleVersion = 0;
  const buttons = [];
  function setBusy(value) {
    busy = value; runButton.disabled = value; fileInput.disabled = value; passInput.disabled = value;
    for (const b of buttons) b.disabled = value;
    progress.setAttribute('aria-busy', String(value));
  }
  async function showSample(sample, focus = false) {
    if (busy) return;
    const version = ++sampleVersion;
    try {
      const report = await loadSample(sample);
      if (busy || version !== sampleVersion) return;
      renderReport(host, report);
      for (const b of buttons) b.setAttribute('aria-pressed', String(b.dataset.sample === sample.id));
      if (focus) host.focus();
    } catch (error) { if (version === sampleVersion) progress.textContent = error.message; }
  }
  for (const sample of SAMPLES) {
    const b = button('', () => showSample(sample, true), 'sample-button');
    b.dataset.sample = sample.id; b.setAttribute('aria-pressed', 'false');
    b.append(el('strong', '', sample.title), el('span', '', sample.detail), el('small', '', 'Explore sample · $0 →'));
    buttons.push(b); document.querySelector('#sample-buttons').append(b);
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    const file = fileInput.files[0];
    errorBox.replaceChildren(); fileInput.removeAttribute('aria-invalid');
    if (!file || !file.name.toLowerCase().endsWith('.xlsx') || file.size > 1_000_000) {
      fileInput.setAttribute('aria-invalid', 'true'); fileInput.focus();
      errorBox.textContent = 'Choose an .xlsx workbook no larger than 1 MB.'; return;
    }
    ++sampleVersion; setBusy(true);
    let report;
    const passphrase = passInput.value; passInput.value = '';
    try {
      const workbook_b64 = await workbookBase64(file);
      let session, outline;
      async function stage(name, payload) {
        const response = await fetch('./api/ek', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({stage: name, workbook_b64, ...(session ? {session} : {}),
            ...(payload ? {payload} : {}), ...(passphrase ? {passphrase} : {})})});
        const body = await response.json();
        if (report && body.session_cost_usd !== undefined) report.session_cost_usd = body.session_cost_usd;
        if (!response.ok) throw new Error(body.message || 'Review unavailable. Use the sample results (cost $0).');
        return body;
      }
      progress.textContent = 'Reading structure · Nemotron Nano…';
      const start = await stage('start'); session = start.session; outline = start.outline;
      report = {workbook: file.name, source: start.source, sheets: start.sheets, outline, session_cost_usd: start.session_cost_usd,
        findings: start.candidates.map(c => ({...c, candidate_id: c.id, verdict: 'not reviewed', reason: 'Waiting for review.', evidence: []}))};
      for (const b of buttons) b.setAttribute('aria-pressed', 'false');
      renderReport(host, report);
      const decisions = [], reviewed = start.candidates.slice(0, start.max_candidates);
      for (const [index, candidate] of reviewed.entries()) {
        progress.textContent = `Candidate ${index + 1} of ${reviewed.length} · ${candidate.sheet}!${candidate.cell} · Nemotron Super…`;
        const result = await stage('judge', {candidate_id: candidate.id, outline});
        decisions.push(result.decision); report.session_cost_usd = result.session_cost_usd;
        report.findings = report.findings.map(f => f.id === candidate.id ? result.decision : f);
        renderReport(host, report);
      }
      progress.textContent = 'Ordering findings · Nemotron Ultra…';
      const result = await stage('summary', {decisions, outline});
      Object.assign(report, {summary: result.summary, findings: result.findings, session_cost_usd: result.session_cost_usd});
      renderReport(host, report); progress.textContent = `Review complete · ${reviewed.length} candidates reviewed.`; host.focus();
    } catch (error) {
      if (report) renderReport(host, report);
      errorBox.textContent = error.message || 'Review unavailable.';
      const link = el('a', '', 'Use the sample results (cost $0)'); link.href = '#samples';
      errorBox.append(document.createTextNode(' '), link);
      progress.textContent = report ? 'Review stopped. Collected findings are preserved.' : 'Live review did not start. Sample results remain available.';
    } finally { setBusy(false); }
  });
  await showSample(SAMPLES[0]);
}

if (document.querySelector('#app-shell')) mountApp();
