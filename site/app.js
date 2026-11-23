/* Public MCSO booking records. Never use innerHTML with remote data. */
const byId = id => document.getElementById(id);
const PAGE_SIZE = 25;
const state = { mode: 'recent', recent: [], archive: null, manifest: null, updated: null, scanned: null, page: 0, error: '' };
const FIELDS = [
  ['swis_id', 'SWIS ID'], ['name', 'Name'], ['age_displayed', 'Age'],
  ['gender', 'Gender'], ['race', 'Race'], ['height', 'Height'],
  ['weight', 'Weight'], ['hair', 'Hair'], ['eyes', 'Eyes'],
  ['arresting_agency', 'Arresting agency'],
  ['booking_datetime_local', 'Booking date and time (Pacific)'],
  ['facility', 'Assigned facility'],
  ['projected_release_date_displayed', 'Projected release date']
];
const fmtDate = day => /^\d{4}-\d{2}-\d{2}$/.test(day || '')
  ? new Date(`${day}T12:00:00`).toLocaleDateString('en-US', {year:'numeric',month:'short',day:'numeric'})
  : 'Date unavailable';
const str = value => value === null || value === undefined || value === '' ? 'Not provided' : String(value);
function text(parent, tag, cls, value) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  el.textContent = value === '' ? '' : str(value);
  parent.append(el);
  return el;
}
async function fetchJSON(file) {
  const response = await fetch(`./${file}`, {cache:'no-store'});
  if (!response.ok) throw new Error(`Could not load ${file} (HTTP ${response.status})`);
  return response.json();
}
function searchText(b) {
  const d = b.booking_detail || {};
  const cases = b.case_groups || [];
  return [b.name, b.booking_id, b.booking_date, b.facility, ...(b.charges || []),
    ...Object.values(d), ...cases.flatMap(c => [c.court_case_number, c.da_case_number,
      c.citation_number, ...(c.charges || []).flatMap(ch => [ch.description, ch.bail_displayed, ch.status_displayed])])]
      .filter(v => v !== null && v !== undefined).join(' ').toLocaleLowerCase();
}
function matched() {
  const term = byId('query').value.trim().toLocaleLowerCase();
  const from = byId('start-day').value, to = byId('end-day').value;
  const items = state.mode === 'recent' ? state.recent : (state.archive || []);
  return items.filter(b => (!from || b.booking_date >= from) && (!to || b.booking_date <= to)
    && (!term || searchText(b).includes(term)));
}
function fullDetails(parent, b) {
  const section = document.createElement('details');
  section.className = 'booking-details';
  text(section, 'summary', '', 'View full booking details, cases and bail');
  const grid = text(section, 'div', 'detail-grid', '');
  const d = b.booking_detail || {};
  for (const [key, label] of FIELDS) {
    const item = text(grid, 'div', 'detail-pair', '');
    text(item, 'span', 'detail-label', label);
    text(item, 'span', 'detail-value', d[key]);
  }
  text(section, 'h3', 'case-heading', 'Court cases and charges');
  const groups = b.case_groups || [];
  if (!groups.length) text(section, 'p', 'muted', 'No charge groups listed in the collected booking detail.');
  for (const c of groups) {
    const group = text(section, 'section', 'case-group', '');
    const caseMeta = text(group, 'div', 'case-meta', '');
    text(caseMeta, 'span', '', `Court case: ${str(c.court_case_number)}`);
    text(caseMeta, 'span', '', `DA case: ${str(c.da_case_number)}`);
    text(caseMeta, 'span', '', `Citation: ${str(c.citation_number)}`);
    const list = document.createElement('ol'); list.className = 'charge-list'; group.append(list);
    for (const ch of (c.charges || [])) {
      const row = text(list, 'li', 'charge-row', '');
      text(row, 'strong', '', ch.description);
      text(row, 'span', '', `Bail: ${str(ch.bail_displayed)}`);
      text(row, 'span', '', `Status: ${str(ch.status_displayed)}`);
    }
  }
  const audit = text(section, 'div', 'audit', '');
  text(audit, 'span', '', `Booking ID: ${b.booking_id}`);
  if (b.captured_utc) text(audit, 'span', '', `Collected: ${new Date(b.captured_utc).toLocaleString('en-US', {timeZone:'America/Los_Angeles'})} Pacific`);
  parent.append(section);
}
function draw() {
  const results = byId('results');
  results.replaceChildren();
  if (state.error) { byId('status').textContent = state.error; return; }
  if (state.mode === 'archive' && !state.archive) return;
  const items = matched();
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages - 1);
  byId('count').textContent = items.length.toLocaleString();
  byId('count-label').textContent = state.mode === 'recent' ? 'Matching recent bookings' : 'Matching archived bookings';
  const lastRun = state.scanned || state.updated;
  const stale = lastRun && Date.now() - lastRun.getTime() > 36 * 3600 * 1000;
  if (!items.length) {
    byId('status').textContent = `${stale ? '⚠️ Collection overdue. ' : ''}No matching bookings in this view. Switch to Search archive to view earlier collected records.`;
    byId('pager').hidden = true;
    return;
  }
  const first = state.page * PAGE_SIZE + 1, last = Math.min(items.length, (state.page + 1) * PAGE_SIZE);
  byId('status').textContent = `${stale ? '⚠️ Collection overdue—verify with MCSO. ' : ''}Showing ${first.toLocaleString()}–${last.toLocaleString()} of ${items.length.toLocaleString()} booking records, newest first.`;
  byId('status').classList.toggle('warning', !!stale);
  const frag = document.createDocumentFragment();
  for (const b of items.slice(first - 1, last)) {
    const card = document.createElement('article'); card.className = 'booking full-booking';
    const main = text(card, 'div', 'booking-main', '');
    text(main, 'h2', 'name', b.name || 'Name unavailable');
    const meta = text(main, 'div', 'meta', '');
    text(meta,'span','',`Booked: ${str(b.booking_detail?.booking_datetime_local || fmtDate(b.booking_date))}`);
    text(meta,'span','',`Booking #${b.booking_id}`);
    if (b.facility) text(meta,'span','',`Facility: ${b.facility}`);
    const charges = text(main, 'div', 'charges', '');
    if ((b.charges || []).length) for (const charge of b.charges) text(charges,'span','charge',charge);
    else text(charges,'span','charge','No charges listed in this snapshot');
    if (/^https:\/\/apps\.mcso\.us\/PAID\/Home\/Booking\?id=\d+(?:&releaseId=\d+)?$/i.test(b.source_url || '')) {
      const link = text(main,'a','official-link','View official MCSO source ↗');
      link.href = b.source_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    }
    fullDetails(main, b);
    frag.append(card);
  }
  results.append(frag);
  byId('pager').hidden = pages < 2;
  byId('page-indicator').textContent = `Page ${state.page + 1} of ${pages}`;
  byId('prev').disabled = state.page === 0;
  byId('next').disabled = state.page >= pages - 1;
}
async function loadArchive() {
  if (state.archive) return;
  byId('status').textContent = 'Loading the complete archive…';
  try {
    const yearFiles = await Promise.all((state.manifest.years || []).map(y => fetchJSON(y.file)));
    state.archive = yearFiles.flatMap(y => Array.isArray(y.bookings) ? y.bookings : []);
    state.archive.sort((a,b) => (b.booking_sort_local || b.booking_date).localeCompare(a.booking_sort_local || a.booking_date) || Number(b.booking_id) - Number(a.booking_id));
    draw();
  } catch (e) {
    state.error = 'Archive could not be loaded. Check the official MCSO website for current information.';
    draw();
  }
}
async function switchMode(mode) {
  state.mode = mode; state.page = 0; state.error = '';
  for (const m of ['recent', 'archive']) {
    byId(`${m}-tab`).classList.toggle('active', m === mode);
    byId(`${m}-tab`).setAttribute('aria-pressed', String(m === mode));
  }
  byId('mode-hint').textContent = mode === 'recent'
    ? 'Bookings dated within the last seven calendar days, newest first.'
    : 'All bookings retained in this archive. Older information may have changed since collection.';
  if (mode === 'archive') await loadArchive(); else draw();
}
async function start() {
  try {
    const [recent, manifest] = await Promise.all([fetchJSON('recent.json'), fetchJSON('archive/manifest.json')]);
    if (!Array.isArray(recent.bookings) || !Array.isArray(manifest.years) || !recent.last_updated_utc) throw new Error('Invalid feed');
    state.recent = recent.bookings;
    state.manifest = manifest;
    state.updated = new Date(recent.last_updated_utc);
    state.scanned = recent.last_scan_utc ? new Date(recent.last_scan_utc) : null;
    if (Number.isNaN(state.updated.getTime())) throw new Error('Invalid date');
    const recentTime = state.scanned && !Number.isNaN(state.scanned.getTime()) ? state.scanned : state.updated;
    byId('updated').textContent = recentTime.toLocaleString('en-US',{timeZone:'America/Los_Angeles',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'});
    byId('coverage').textContent = manifest.coverage_start_utc ? fmtDate(manifest.coverage_start_utc.slice(0,10)) : 'Not available';
    draw();
  } catch (e) {
    state.error = 'The public booking feed is temporarily unavailable. Check the official MCSO source.';
    draw();
  }
}
byId('recent-tab').addEventListener('click', () => switchMode('recent'));
byId('archive-tab').addEventListener('click', () => switchMode('archive'));
for (const id of ['query', 'start-day', 'end-day']) byId(id).addEventListener('input', () => {state.page = 0; draw();});
byId('clear').addEventListener('click', () => {
  for (const id of ['query', 'start-day', 'end-day']) byId(id).value = '';
  state.page = 0; draw();
});
byId('prev').addEventListener('click', () => {state.page = Math.max(0, state.page - 1); draw(); window.scrollTo(0, 270);});
byId('next').addEventListener('click', () => {state.page++; draw(); window.scrollTo(0, 270);});
start();
