(() => {
  'use strict';
  const config = window.VIVIAN_CONFIG || {};
  const status = document.getElementById('profile-message');
  const guildInput = document.getElementById('profile-guild');
  const memberInput = document.getElementById('profile-member');
  const root = document.getElementById('public-profile');
  const route = window.location.pathname.match(/^\/profile\/([1-9][0-9]{0,19})\/([1-9][0-9]{0,19})$/);
  const query = new URLSearchParams(window.location.search);
  guildInput.value = route?.[1] || query.get('guild') || '';
  memberInput.value = route?.[2] || query.get('member') || query.get('user') || '';
  const site = config.publicSiteUrl || 'https://esil.life';
  document.querySelectorAll('[data-home]').forEach(anchor => { anchor.href = site; });
  document.getElementById('dashboard-home').href = route ? '/dashboard' : 'dashboard.html';
  let pending = 0;

  function profilePath(guild, member) {
    return route ? `/profile/${guild}/${member}` : `?guild=${guild}&member=${member}`;
  }
  function memberLink(guild, member) {
    const link = document.createElement('a');
    link.textContent = member.display_name;
    link.href = profilePath(guild, member.id);
    return link;
  }
  function appendMetric(name, value) {
    const card = document.createElement('div'); card.className = 'hub-metric';
    const number = document.createElement('strong'); number.textContent = Number(value).toLocaleString();
    const label = document.createElement('span'); label.textContent = name; card.append(number, label);
    document.getElementById('profile-metrics').append(card);
  }
  async function load() {
    const requestId = ++pending;
    root.hidden = true;
    const guild = guildInput.value.trim(), member = memberInput.value.trim();
    if (!/^[1-9][0-9]{0,19}$/.test(guild) || !/^[1-9][0-9]{0,19}$/.test(member)) {
      status.textContent = 'Enter valid server and member IDs.'; return;
    }
    try {
      const base = route ? window.location.origin : config.apiBase;
      if (!base) throw new Error('Set apiBase in config.js to the bot\'s existing Render service URL.');
      const origin = new URL(base);
      if (origin.protocol !== 'https:' || origin.username || origin.password || origin.pathname !== '/' || origin.search || origin.hash) {
        throw new Error('The profile API must use an HTTPS origin without a path.');
      }
      const endpoint = new URL(`/api/public/guilds/${guild}/members/${member}`, origin);
      status.textContent = 'Loading public profile…';
      const response = await fetch(endpoint, { credentials: 'omit' });
      let data;
      try { data = await response.json(); } catch { throw new Error('The bot service returned an invalid profile response.'); }
      if (requestId !== pending) return;
      if (!response.ok) throw new Error(data.error || 'This profile is not public.');
      document.getElementById('profile-name').textContent = data.display_name;
      document.title = `${data.display_name} · Vivian`;
      document.getElementById('profile-server').textContent = data.guild.name;
      document.getElementById('profile-status').textContent = [data.mood, data.status_text].filter(Boolean).join(' ');
      const avatar = new URL(data.avatar_url);
      const avatarImage = document.getElementById('profile-avatar');
      avatarImage.removeAttribute('src');
      if (avatar.protocol === 'https:' && ['cdn.discordapp.com', 'media.discordapp.net'].includes(avatar.hostname)) {
        avatarImage.src = avatar.href;
      }
      document.getElementById('profile-metrics').replaceChildren();
      appendMetric('Level', data.level); appendMetric('Messages', data.messages);
      appendMetric('Total XP', data.total_xp); appendMetric('Friends', data.friend_count);
      const progress = document.getElementById('profile-progress');
      progress.max = Math.max(1, Number(data.xp_required)); progress.value = Math.max(0, Number(data.xp_into));
      document.getElementById('profile-xp').textContent = `${data.xp_into} / ${data.xp_required} XP toward the next level`;
      const badges = document.getElementById('profile-badges'); badges.replaceChildren();
      const badgeNames = [...new Set([...(data.badges || []), ...(data.economy?.badges || [])])];
      badgeNames.forEach(text => { const badge = document.createElement('span'); badge.className = 'profile-badge'; badge.textContent = text; badges.append(badge); });
      const relationship = document.getElementById('profile-relationship'); relationship.replaceChildren();
      if (data.relationship) {
        relationship.append(document.createTextNode('Together with '), memberLink(guild, data.relationship));
      }
      const friends = document.getElementById('profile-friend-list'); friends.replaceChildren();
      (data.friends || []).forEach(friend => {
        const item = document.createElement('li');
        item.className = 'profile-badge'; item.append(memberLink(guild, friend)); friends.append(item);
      });
      document.getElementById('profile-friends').hidden = !friends.childElementCount;
      const wall = document.getElementById('profile-wall'); wall.replaceChildren();
      (data.wall || []).forEach(post => {
        const card = document.createElement('article');
        const text = document.createElement('p'); text.textContent = post.text;
        const likes = document.createElement('small'); likes.textContent = `${post.likes} ${post.likes === 1 ? 'like' : 'likes'}`;
        card.append(memberLink(guild, post.author), text, likes);
        if (post.comments?.length) {
          const comments = document.createElement('ul');
          comments.className = 'profile-comments';
          post.comments.forEach(comment => {
            const entry = document.createElement('li');
            entry.append(memberLink(guild, comment.author), document.createTextNode(` · ${comment.text}`));
            comments.append(entry);
          });
          card.append(comments);
        }
        wall.append(card);
      });
      document.getElementById('profile-wall-section').hidden = !wall.childElementCount;
      root.hidden = false; status.textContent = 'Published by this member.';
      history.replaceState(null, '', profilePath(guild, member));
    } catch (error) {
      if (requestId === pending) status.textContent = error.message;
    }
  }
  document.getElementById('profile-search').addEventListener('submit', event => { event.preventDefault(); load(); });
  if (guildInput.value && memberInput.value) load();
})();
