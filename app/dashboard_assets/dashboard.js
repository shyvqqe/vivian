(() => {
  'use strict';
  const status = document.getElementById('page-status');
  const selector = document.getElementById('guild-select');
  const groups = document.getElementById('settings-groups');
  const saveButton = document.getElementById('save-settings');
  const controls = new Map();
  let csrf = '', current = null, loadedGuild = '';
  const config = window.VIVIAN_CONFIG || {};
  const website = config.publicSiteUrl || 'https://esil.life';
  document.querySelectorAll('[data-home]').forEach(anchor => { anchor.href = website; });
  document.getElementById('profile-home').href = website.replace(/\/$/, '') + '/profile.html';
  function message(text) { status.textContent = text; }
  function label(key) { return key.replace(/_id$|_ids$/, '').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()); }
  async function request(path, options = {}) {
    const response = await fetch(path, { credentials: 'same-origin', ...options });
    let value;
    try { value = await response.json(); } catch { throw new Error('The dashboard returned an invalid response.'); }
    if (!response.ok) {
      if (response.status === 401) document.getElementById('login-panel').hidden = false;
      throw new Error(value.error || 'The request could not be completed.');
    }
    return value;
  }
  function option(select, value, text) {
    const item = document.createElement('option');
    item.value = value; item.textContent = text; select.append(item);
  }
  function buildFields(data) {
    current = data.settings;
    groups.replaceChildren(); controls.clear();
    const sections = new Map();
    function section(name) {
      if (!sections.has(name)) {
        const set = document.createElement('fieldset'); set.className = 'hub-group';
        const legend = document.createElement('legend'); legend.textContent = name; set.append(legend);
        groups.append(set); sections.set(name, set);
      }
      return sections.get(name);
    }
    for (const [key, value] of Object.entries(current)) {
      const row = document.createElement('label'); row.className = 'hub-field';
      const title = document.createElement('span'); title.textContent = label(key); row.append(title);
      let input;
      const category = key.endsWith('_enabled') && !key.startsWith('moderate_') ? 'Community modules'
        : key.endsWith('_id') || key.endsWith('_ids') ? 'Channels and roles'
        : key.startsWith('moderate_') || key.startsWith('jail_') ? 'Moderation'
        : /currency|reward|season|style/.test(key) ? 'Economy and appearance' : 'Schedules';
      if (typeof value === 'boolean') {
        input = document.createElement('input'); input.type = 'checkbox'; input.checked = value;
        row.classList.add('hub-toggle');
      } else if (key.endsWith('_id') || key.endsWith('_ids')) {
        input = document.createElement('select');
        input.multiple = key.endsWith('_ids');
        if (!input.multiple) option(input, '', 'Not configured');
        const choices = key.includes('role') ? data.roles.filter(role => !role.managed) : data.channels.filter(channel => channel.kind === (key.startsWith('voice_') ? 'voice' : 'text'));
        choices.forEach(item => option(input, item.id, `${key.includes('role') ? '@' : '#'}${item.name}`));
        const selected = new Set(Array.isArray(value) ? value.map(String) : value === null ? [] : [String(value)]);
        [...input.options].forEach(item => { item.selected = selected.has(item.value); });
      } else if (Array.isArray(value) || (value !== null && typeof value === 'object')) {
        input = document.createElement('textarea'); input.value = JSON.stringify(value, null, 2);
      } else {
        input = document.createElement('input'); input.type = typeof value === 'number' ? 'number' : 'text';
        if (input.type === 'number') input.step = '1';
        input.value = value ?? '';
      }
      input.name = key; row.append(input); section(category).append(row); controls.set(key, input);
    }
    const metrics = document.getElementById('guild-stats'); metrics.replaceChildren();
    for (const [name, value] of Object.entries({ Members: data.guild.member_count, Messages: data.stats.messages,
      'Tracked members': data.stats.tracked_members, Commands: data.stats.commands })) {
      const card = document.createElement('div'); card.className = 'hub-metric';
      const number = document.createElement('strong'); number.textContent = Number(value).toLocaleString();
      const text = document.createElement('span'); text.textContent = name; card.append(number, text); metrics.append(card);
    }
  }
  async function loadGuild() {
    const id = selector.value;
    if (!id) return;
    loadedGuild = ''; saveButton.disabled = true; message('Checking Discord permissions and loading settings…');
    try {
      const data = await request(`/api/guilds/${encodeURIComponent(id)}`);
      if (selector.value !== id) return;
      loadedGuild = id; buildFields(data); message(`Managing ${data.guild.name}`); saveButton.disabled = false;
      document.getElementById('save-notice').textContent = 'Changes apply after saving.';
    } catch (error) { message(error.message); }
  }
  selector.addEventListener('change', loadGuild);
  document.getElementById('reload-guild').addEventListener('click', loadGuild);
  document.getElementById('settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!current || !loadedGuild || loadedGuild !== selector.value) return;
    const patch = {};
    try {
      for (const [key, input] of controls) {
        let value;
        if (typeof current[key] === 'boolean') value = input.checked;
        else if (key.endsWith('_ids')) value = [...input.selectedOptions].map(option => option.value);
        else if (key.endsWith('_id')) value = input.value || null;
        else if (Array.isArray(current[key]) || (current[key] !== null && typeof current[key] === 'object')) value = JSON.parse(input.value);
        else if (typeof current[key] === 'number') {
          value = Number(input.value);
          if (!Number.isSafeInteger(value)) throw new Error(`${label(key)} must be a whole number.`);
        } else value = input.value;
        if (JSON.stringify(value) !== JSON.stringify(current[key])) patch[key] = value;
      }
      if (!Object.keys(patch).length) { message('No settings changed.'); return; }
      if (Object.keys(patch).length > 30) throw new Error('Save at most 30 changed settings at a time.');
      const id = loadedGuild; saveButton.disabled = true; selector.disabled = true;
      message('Validating channels, roles and permissions…');
      const result = await request(`/api/guilds/${encodeURIComponent(id)}/settings`, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ patch }) });
      current = result.settings; message('Settings saved.');
      document.getElementById('save-notice').textContent = 'All changes were saved to persistent storage.';
    } catch (error) { message(error.message); }
    finally { saveButton.disabled = !loadedGuild; selector.disabled = false; }
  });
  document.getElementById('sign-out').addEventListener('click', async () => {
    try { await request('/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } }); window.location.reload(); }
    catch (error) { message(error.message); }
  });
  (async () => {
    try {
      const session = await request('/api/session'); csrf = session.csrf;
      document.getElementById('sign-out').hidden = false;
      const result = await request('/api/guilds');
      if (!result.guilds.length) { message('No manageable servers found. Add Vivian to a server and check Manage Server permission.'); return; }
      result.guilds.forEach(guild => option(selector, guild.id, guild.name));
      document.getElementById('dashboard-panel').hidden = false; await loadGuild();
    } catch (error) { message(error.message); }
  })();
})();
