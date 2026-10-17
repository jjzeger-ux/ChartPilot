/* Explicit encounter binding prevents late requests from writing to another visit. */
(() => {
  const kind = location.pathname === '/scribe' ? 'scribe' : 'intake';
  const originalFetch = window.fetch.bind(window);
  let encounterId = null, dirty = {}, timer = null, saving = Promise.resolve(), failed = false, pendingCount = 0;
  const status = document.getElementById('saveStatus');
  const setStatus = text => { if (status) status.textContent = text; };
  const fields = [...document.querySelectorAll('.note-card textarea')];
  const transcript = document.getElementById('transcript');
  const startButton = document.getElementById('btnStart');
  startButton.disabled = true;
  window.fetch = (url, options = {}) => {
    const headers = new Headers(options.headers);
    if (encounterId && String(url).startsWith('/')) headers.set('X-Hermes-Encounter', encounterId);
    return originalFetch(url, {...options, headers});
  };
  async function request(url, options) {
    const response = await window.fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Request failed.');
    return data;
  }
  function flush() {
    clearTimeout(timer);
    if (!encounterId || !Object.keys(dirty).length) return saving;
    const pending = dirty; dirty = {}; const id = encounterId;
    const body = {kind, fields:{}};
    for (const [key,value] of Object.entries(pending)) {
      if (key === 'transcript') body.transcript = value; else body.fields[key] = value;
    }
    setStatus('Saving…'); pendingCount++;
    saving = saving.catch(() => {}).then(async () => {
      try { await request(`/api/encounters/${id}/notes`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        failed = false; setStatus(Object.keys(dirty).length ? 'Unsaved changes' : 'All changes saved');
      } catch (error) { dirty = {...pending, ...dirty}; failed = true; setStatus('Not saved — click to retry'); throw error; }
      finally { pendingCount--; }
    });
    return saving;
  }
  function changed(event) { dirty[event.target.id] = event.target.value; setStatus('Unsaved changes'); clearTimeout(timer); timer = setTimeout(() => flush().catch(() => {}), 650); }
  fields.forEach(field => field.addEventListener('input', changed));
  transcript.addEventListener('input', changed);
  status?.addEventListener('click', () => flush().catch(() => {}));
  async function openEncounter(id) {
    await flush();
    const enc = await request(`/api/encounters/${id}`);
    await request(`/api/encounters/${id}/select`, {method:'POST'});
    encounterId = id;
    const note = enc[kind + '_note'] || {};
    fields.forEach(field => field.value = note[field.id] || '');
    transcript.value = enc.transcript || '';
    whisperText = transcript.value; speechFinal = ''; speechInterim = '';
    if (typeof activeEid !== 'undefined') { activeEid = id; await refreshQueue(); }
    history.replaceState(null, '', `${location.pathname}?encounter=${encodeURIComponent(id)}`);
    document.getElementById('encounterLabel').textContent = enc.mrn;
    setStatus('All changes saved');
  }
  const ready = (async () => {
    let id = new URLSearchParams(location.search).get('encounter');
    if (!id) { const data = await request('/api/notes'); id = data.patient?.id; }
    if (!id) id = (await request('/api/encounters', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).id;
    await openEncounter(id); startButton.disabled = false;
  })().catch(error => { failed = true; setStatus('Unable to open encounter'); toast(error.message, 'err'); throw error; });
  ready.catch(() => {});
  const startName = kind === 'intake' ? 'startEncounter' : 'startRecording';
  const originalStart = window[startName];
  window[startName] = async () => { try { await ready; await flush(); await originalStart(); } catch(e) { toast(e.message, 'err'); } };
  const originalClear = window.clearAll;
  window.clearAll = async () => { try { await flush(); await originalClear(); } catch(e) { toast('Save your changes before clearing.', 'err'); } };
  const originalGenerate = window.generateNote;
  window.generateNote = async () => {
    dirty.transcript = transcript.value;
    try { await flush(); await originalGenerate(); }
    catch(error) { stopProcTimer(); hideSkeletons(); setBtns('idle'); applyState('idle'); toast('Transcript could not be saved. Retry saving before generating a note.', 'err'); }
  };
  window.HermesWorkspace = {ready, flush, openEncounter};
  document.querySelectorAll('a[href]').forEach(link => {
    if (link.target === '_blank') return;
    link.addEventListener('click', async event => {
      if (encounterId && startButton.disabled) { event.preventDefault(); toast('Finish the recording and note generation before leaving.', 'err'); return; }
      if (!Object.keys(dirty).length && !failed && !pendingCount) return;
      event.preventDefault();
      try { await flush(); location.href = link.href; } catch(e) { toast('Your changes are not saved. Click the save status to retry.', 'err'); }
    });
  });
  window.addEventListener('beforeunload', event => {
    if (Object.keys(dirty).length || failed || pendingCount || (encounterId && startButton.disabled)) { event.preventDefault(); event.returnValue = ''; }
  });
})();
