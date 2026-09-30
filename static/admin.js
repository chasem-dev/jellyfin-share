const $ = (id) => document.getElementById(id);
let selected = null;
let selectedDetail = null;
let catalogTimer;

async function json(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error((await response.text()) || `Request failed (${response.status})`);
  return response.json();
}
function formatDate(seconds) { return new Date(seconds * 1000).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'}); }
function node(tag, className, text) { const element = document.createElement(tag); element.className = className; element.textContent = text; return element; }
function updateScope() {
  const scope = selected?.kind === 'movie' ? 'movie' : $('scope').value;
  $('season-wrap').hidden = scope !== 'season';
  $('episode-wrap').hidden = scope !== 'episode';
  let detail = '';
  if (scope === 'season') detail = $('season').selectedOptions[0]?.textContent || '';
  if (scope === 'episode') detail = $('episode').selectedOptions[0]?.textContent || '';
  $('selection').textContent = selected ? `${selected.title}${detail ? ` · ${detail}` : ` · ${selected.kind === 'movie' ? 'Movie' : `${selected.count} episodes`}`}` : 'Select a title from the library.';
  $('created').hidden = true;
}
async function choose(item) {
  selected = item; selectedDetail = null; $('selection').classList.remove('empty-selection'); $('created').hidden = true;
  $('form-error').textContent = '';
  $('scope-controls').hidden = item.kind === 'movie'; $('create').disabled = item.kind !== 'movie';
  $('scope').value = 'series'; updateScope(); loadCatalog();
  if (item.kind === 'movie') return;
  try {
    const detail = await json(`/api/catalog/${item.id}`);
    if (selected?.id !== item.id) return;
    selectedDetail = detail;
    $('season').replaceChildren(); $('episode').replaceChildren();
    for (const season of detail.seasons) {
      const option = new Option(`${season.label} · ${season.count} episode${season.count === 1 ? '' : 's'}`, season.number);
      $('season').append(option);
      const group = document.createElement('optgroup'); group.label = season.label;
      for (const episode of season.episodes) group.append(new Option(episode.label, episode.id));
      $('episode').append(group);
    }
    $('create').disabled = !detail.seasons.length; updateScope();
  } catch { $('form-error').textContent = 'This series could not be loaded. Refresh the library and try again.'; }
}
async function copy(text, button) {
  try { await navigator.clipboard.writeText(text); button.textContent = 'Copied'; setTimeout(() => button.textContent = 'Copy', 1600); }
  catch { window.prompt('Copy this link', text); }
}
async function loadCatalog(refresh = false) {
  $('catalog').replaceChildren(node('p', 'muted', 'Looking through your library…'));
  try {
    const query = $('search').value.trim();
    const items = await json(`/api/catalog?q=${encodeURIComponent(query)}${refresh ? '&refresh=1' : ''}`);
    const list = $('catalog'); list.replaceChildren();
    if (!items.length) { list.append(node('p', 'muted', 'No matching titles found.')); return; }
    for (const item of items) {
      const button = node('button', `catalog-item${selected?.id === item.id ? ' selected' : ''}`, '');
      button.type = 'button';
      const icon = node('span', 'item-icon', item.kind === 'movie' ? '▣' : '▤');
      const info = node('span', 'item-info', '');
      info.append(node('strong', '', item.title), node('small', '', item.kind === 'movie' ? 'Movie' : `Series · ${item.count} episode${item.count === 1 ? '' : 's'}`));
      button.append(icon, info, node('span', 'item-arrow', '↗'));
      button.addEventListener('click', () => choose(item));
      list.append(button);
    }
  } catch { $('catalog').replaceChildren(node('p', 'error', 'Library could not be loaded.')); }
}
async function loadShares() {
  try {
    const shares = await json('/api/shares'); const list = $('shares'); list.replaceChildren();
    if (!shares.length) { list.append(node('p', 'muted', 'No links yet. Create one above.')); return; }
    for (const share of shares) {
      const active = !share.revoked_at && share.expires_at > Date.now() / 1000;
      const row = node('div', 'share-row', '');
      const content = node('div', 'share-info', '');
      const display = `${share.title}${share.scope_label ? ` · ${share.scope_label}` : ''}`;
      content.append(node('strong', '', display), node('small', '', active ? `Expires ${formatDate(share.expires_at)}` : share.revoked_at ? 'Revoked' : 'Expired'));
      row.append(content);
      const status = node('span', `status ${active ? 'active' : ''}`, active ? 'Active' : share.revoked_at ? 'Revoked' : 'Expired'); row.append(status);
      if (active) {
        const copyButton = node('button', 'small-button', 'Copy'); copyButton.type = 'button'; copyButton.addEventListener('click', () => copy(share.url, copyButton)); row.append(copyButton);
        const revokeButton = node('button', 'small-button danger', 'Revoke'); revokeButton.type = 'button'; revokeButton.addEventListener('click', async () => { if (!confirm(`Revoke the link for ${display}?`)) return; await json(`/api/shares/${share.id}/revoke`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); loadShares(); }); row.append(revokeButton);
      }
      list.append(row);
    }
  } catch { $('shares').replaceChildren(node('p', 'error', 'Links could not be loaded.')); }
}
$('search').addEventListener('input', () => { clearTimeout(catalogTimer); catalogTimer = setTimeout(() => loadCatalog(), 200); });
$('refresh').addEventListener('click', () => loadCatalog(true));
$('scope').addEventListener('change', updateScope);
$('season').addEventListener('change', updateScope);
$('episode').addEventListener('change', updateScope);
$('create').addEventListener('click', async () => {
  if (!selected) return;
  $('form-error').textContent = ''; $('create').disabled = true;
  try {
    const scope = selected.kind === 'movie' ? 'movie' : $('scope').value;
    const payload = {catalog_id: selected.id, hours: Number($('duration').value), scope};
    if (scope === 'season') payload.season = Number($('season').value);
    if (scope === 'episode') payload.episode_id = $('episode').value;
    const result = await json('/api/shares', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    $('created-url').value = result.url; $('created').hidden = false; await loadShares();
  } catch (error) { $('form-error').textContent = error.message; }
  finally { $('create').disabled = false; }
});
$('copy-created').addEventListener('click', () => copy($('created-url').value, $('copy-created')));
loadCatalog(); loadShares();
