/* Plain-text rendering only: incoming scraped text can never execute as HTML. */
const byId = id => document.getElementById(id);
const PAGE_SIZE = 50;
const state = { mode: 'recent', recent: [], archive: null, manifest: null, updated: null, page: 0, error: '' };
const fmtDate = day => /^\d{4}-\d{2}-\d{2}$/.test(day || '')
  ? new Date(`${day}T12:00:00`).toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'})
  : 'Date unavailable';
function text(parent, tag, cls, value) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  el.textContent = value;
  parent.append(el);
  return el;
}
async function fetchJSON(file) {
  const response = await fetch(`./${file}`, {cache:'no-store'});
  if (!response.ok) throw new Error(`Could not load ${file} (HTTP ${response.status})`);
  return await response.json();
}
function matched() {
  const term = byId('query').value.trim().toLocaleLowerCase();
  const from = byId('start-day').value;
  const to = byId('end-day').value;
  const items = state.mode === 'recent' ? state.recent : (state.archive || []);
  return items.filter(b => (!from || b.booking_date >= from) && (!to || b.booking_date <= to)
    && (!term || [b.name, ...(b.charges || [])].join(' ').toLocaleLowerCase().includes(term)));
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
  if (!items.length) {
    byId('status').textContent = 'No matching bookings in the selected period. The archive only covers dates collected by this service.';
    byId('pager').hidden = true;
    return;
  }
  const first = state.page * PAGE_SIZE + 1, last = Math.min(items.length, (state.page + 1) * PAGE_SIZE);
  const stale = state.updated && Date.now() - state.updated.getTime() > 36 * 3600 * 1000;
  byId('status').textContent = `${stale ? '⚠️ Collection is overdue — verify with MCSO. ' : ''}Showing ${first.toLocaleString()}–${last.toLocaleString()} of ${items.length.toLocaleString()} booking records, newest first.`;
  byId('status').classList.toggle('warning', !!stale);
  const frag = document.createDocumentFragment();
  for (const b of items.slice(first - 1, last)) {
    const card = document.createElement('article'); card.className = 'booking';
    const main = document.createElement('div');
    text(main,'h2','name',b.name || 'Name not published');
    const meta = text(main,'div','meta','');
    text(meta,'span','',`Booked: ${fmtDate(b.booking_date)}`);
    if (b.facility) text(meta,'span','',`Facility: ${b.facility}`);
    const charges = text(main,'div','charges','');
    if ((b.charges || []).length) for (const charge of b.charges) text(charges,'span','charge',charge);
    else text(charges,'span','charge','Charges: see official record');
    card.append(main);
    if (/^https:\/\/apps\.mcso\.us\/PAID\/Home\/Booking(?:\/\d+\/?|\?id=\d+(?:&releaseId=\d+)?)$/.test(b.source_url || '')) {
      const link = text(card,'a','','Official record ↗');
      link.href = b.source_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    }
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
  byId('status').textContent = 'Loading historical records…';
  try {
    const years = state.manifest.years || [];
    const yearFiles = await Promise.all(years.map(y => fetchJSON(y.file)));
    const records = yearFiles.flatMap(y => Array.isArray(y.bookings) ? y.bookings : []);
    // Server exports are already newest-first, but sort to remain robust.
    records.sort((a, b) => b.booking_date.localeCompare(a.booking_date) || Number(b.booking_id) - Number(a.booking_id));
    state.archive = records;
    draw();
  } catch (e) {
    state.error = 'The historical archive could not be loaded. Please retry or check the official MCSO source.';
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
    ? 'Showing bookings collected in the last seven days, newest first.'
    : 'Searching all retained records from the first successful collection onward. Older cases may have changed.';
  if (mode === 'archive') await loadArchive(); else draw();
}
async function start() {
  try {
    const [recent, manifest] = await Promise.all([fetchJSON('recent.json'), fetchJSON('archive/manifest.json')]);
    if (!Array.isArray(recent.bookings) || !Array.isArray(manifest.years) || !recent.last_updated_utc) throw new Error('Invalid feed');
    state.recent = recent.bookings;
    state.manifest = manifest;
    state.updated = new Date(recent.last_updated_utc);
    if (Number.isNaN(state.updated.getTime())) throw new Error('Invalid date');
    byId('updated').textContent = state.updated.toLocaleString('en-US',{timeZone:'America/Los_Angeles',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'});
    byId('coverage').textContent = manifest.coverage_start_utc ? fmtDate(manifest.coverage_start_utc.slice(0,10)) : 'Not available';
    draw();
  } catch (e) {
    state.error = 'No verified public booking feed is published yet. The collector and private archive must be configured and successfully tested.';
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
