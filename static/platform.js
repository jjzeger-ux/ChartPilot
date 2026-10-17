const loginForm = document.getElementById('loginForm');
if (loginForm) loginForm.addEventListener('submit', async event => {
  event.preventDefault(); const button = document.getElementById('loginButton');
  const error = document.getElementById('loginError'); error.textContent = ''; button.disabled = true;
  try { const response = await fetch('/unlock', {method:'POST', body:new FormData(loginForm)});
    const data = await response.json(); if (!response.ok) throw Error(data.error || 'Unable to sign in.');
    location.href = data.next;
  } catch (e) { error.textContent = e.message || 'Connection unavailable. Try again.'; button.disabled = false; }
});
document.querySelectorAll('[data-start]').forEach(button => button.addEventListener('click', async () => {
  document.querySelectorAll('[data-start]').forEach(b => b.disabled = true);
  const error = document.getElementById('dashboardError'); error.textContent = '';
  try { const response = await fetch('/api/encounters', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const data = await response.json(); if (!response.ok) throw Error(data.error || 'Encounter could not be created.');
    location.href = `/${button.dataset.start}?encounter=${encodeURIComponent(data.id)}`;
  } catch(e) { error.textContent = e.message; document.querySelectorAll('[data-start]').forEach(b => b.disabled = false); }
}));
document.getElementById('encounterSearch')?.addEventListener('input', event => {
  let count = 0; document.querySelectorAll('[data-search]').forEach(row => {
    row.hidden = !row.dataset.search.includes(event.target.value.toLowerCase()); if (!row.hidden) count++;
  });
  const empty = document.getElementById('noSearchResults'); if (empty) empty.hidden = count > 0;
});
