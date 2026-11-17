'use strict';
/* Chat Spike Clipper dashboard. Talks to clipper/ui/server.py on this PC. */

const TOKEN = window.CLIPPER_TOKEN;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function hms(t, short = false) {
  t = Math.max(0, Math.floor(t || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  if (short && !h) return `${m}:${String(s).padStart(2, '0')}`;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(val));
  } catch { return null; }
}

async function api(path, { method = 'GET', body } = {}) {
  const r = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Clipper-Token': TOKEN },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ''), err ? 7000 : 3500);
}

// --------------------------------------------------------------------------- state
const S = {
  info: null,
  tab: 'vod',
  sessions: [],
  jobs: {},                       // id -> summary
  jc: {},                         // id -> {log, events, logNext, evNext, d}
  sel: { vod: { job: null, session: null }, live: { job: null, session: null }, captions: { job: null, session: null } },
  sess: {},                       // name -> session data
  unpicked: {},                   // session name -> Set of ranks not to cut
  offline: false,
};

// --------------------------------------------------------------------------- form options
const LAYOUTS = [['center', 'Center crop'], ['stacked', 'Facecam on top']];
const F = {
  multiplier: { label: 'Sensitivity', type: 'num', step: 0.1, min: 1,
    help: 'A spike must be this many times busier than the chat around it. Lower = more clips, higher = only the biggest moments.' },
  max_clips_vod: { k: 'max_clips', label: 'Max clips', type: 'num', step: 1, min: 1, help: 'Keep the top N moments.' },
  max_clips_live: { k: 'max_clips', label: 'Max clips (0 = no limit)', type: 'num', step: 1, min: 0 },
  layout: { label: 'Layout', type: 'select', choices: LAYOUTS,
    help: 'Facecam on top needs the streamer\'s facecam position (Setup tab). Without it, center crop is used.' },
  pre_roll: { label: 'Seconds before spike', type: 'num', step: 1, min: 0, help: 'Chat reacts after the moment, so clips start this long before the spike.' },
  post_roll: { label: 'Seconds after spike', type: 'num', step: 1, min: 0 },
  min_score: { label: 'Minimum spike score', type: 'num', step: 1, min: 0,
    help: 'Ignore spikes smaller than this, however far above normal. Raise it for big channels, lower it for small ones.' },
  merge_gap: { label: 'Merge spikes within (s)', type: 'num', step: 1, min: 0 },
  min_len: { label: 'Shortest clip (s)', type: 'num', step: 1, min: 1 },
  max_len: { label: 'Longest clip (s)', type: 'num', step: 1, min: 1 },
  method: { label: 'Detection method', type: 'select', choices: [['ratio', 'Ratio (recommended)'], ['zscore', 'Z-score']] },
  z: { label: 'Z-score threshold', type: 'num', step: 0.1, min: 0, help: 'Only used with the Z-score method.' },
  rank_by: { label: 'Rank moments by', type: 'select', choices: [['score', 'Biggest chat reaction'], ['ratio', 'Most above normal']],
    help: '"Most above normal" spreads clips across quiet and busy parts of the stream.' },
  window: { label: 'Window (s)', type: 'num', step: 1, min: 2 },
  baseline: { label: 'Baseline span (s)', type: 'num', step: 10, min: 30, help: 'How much surrounding chat counts as "normal".' },
  per_user_cap: { label: 'Messages per user per window', type: 'num', step: 1, min: 0, placeholder: 'from keywords.yaml' },
  quality_vod: { k: 'quality', label: 'Video quality', type: 'text', placeholder: 'source (e.g. 720p60)' },
  quality_live: { k: 'quality', label: 'Video quality', type: 'text', placeholder: 'best (e.g. 720p60)' },
  streamer: { label: 'Facecam name', type: 'text', placeholder: 'auto (channel name)',
    help: 'Which saved facecam position to use. Leave as auto to use the channel name.' },
  downloader: { label: 'Downloader', type: 'select', choices: [['auto', 'Auto'], ['twitchdownloader', 'TwitchDownloaderCLI'], ['yt-dlp', 'yt-dlp']] },
  refresh_chat: { label: 'Re-download chat', type: 'bool' },
  keep_raw: { label: 'Keep raw video', type: 'bool' },
  max_per_hour: { label: 'Max clips per hour (0 = no limit)', type: 'num', step: 1, min: 0 },
  stop_after: { label: 'Stop after (minutes, 0 = never)', type: 'num', step: 10, min: 0 },
  vod_after: { label: 'Also clip the VOD when the stream ends', type: 'bool',
    help: 'Ranks the whole stream from its VOD and clips the best moments that weren\'t clipped live.' },
  video_delay: { label: 'Video delay (s)', type: 'num', step: 1, help: 'If clips start too early, the video lags chat. Shift clips later by this much.' },
  buffer: { label: 'Video kept on disk (s)', type: 'num', step: 30, min: 120 },
  vod_max_clips: { label: 'VOD follow-up max clips', type: 'num', step: 1, min: 1 },
  captions: { label: 'Add captions', type: 'bool',
    help: 'Burns speech captions into each clip, transcribed on this PC. Style on the Captions page. Adds roughly 10–30 s per clip.' },
};
const LAYOUT_OPTS = {
  vod: {
    basic: ['multiplier', 'max_clips_vod', 'layout', 'pre_roll', 'post_roll', 'captions'],
    adv: ['min_score', 'merge_gap', 'min_len', 'max_len', 'rank_by', 'method', 'z', 'window', 'baseline',
      'per_user_cap', 'quality_vod', 'streamer', 'downloader', 'refresh_chat', 'keep_raw'],
  },
  live: {
    basic: ['multiplier', 'min_score', 'layout', 'max_per_hour', 'stop_after', 'captions', 'vod_after'],
    adv: ['max_clips_live', 'merge_gap', 'pre_roll', 'post_roll', 'min_len', 'max_len', 'video_delay', 'method', 'z',
      'quality_live', 'streamer', 'vod_max_clips', 'buffer', 'window', 'baseline', 'per_user_cap', 'keep_raw'],
  },
};

function fieldHTML(mode, id, defs = F) {
  const f = defs[id], k = f.k || id;
  const name = `${mode}-${k}`;
  const help = f.help ? `<span class="help" title="${esc(f.help)}">?</span>` : '';
  if (f.type === 'bool') {
    return `<label class="field check"><input type="checkbox" data-k="${k}" id="${name}"><span>${esc(f.label)}</span>${help}</label>`;
  }
  let input;
  if (f.type === 'select') {
    input = `<select data-k="${k}" id="${name}">${f.choices.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select>`;
  } else if (f.type === 'color') {
    input = `<input type="color" data-k="${k}" id="${name}">`;
  } else if (f.type === 'num') {
    input = `<input type="number" data-k="${k}" id="${name}" step="${f.step ?? 'any'}"${f.min !== undefined ? ` min="${f.min}"` : ''}${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ''}>`;
  } else {
    input = `<input type="text" data-k="${k}" id="${name}" spellcheck="false"${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ''}>`;
  }
  return `<label class="field"><span>${esc(f.label)}${help}</span>${input}</label>`;
}

function buildForm(mode) {
  const L = LAYOUT_OPTS[mode];
  $(`#${mode}Basic`).innerHTML = L.basic.map((id) => fieldHTML(mode, id)).join('');
  $(`#${mode}Adv`).innerHTML = L.adv.map((id) => fieldHTML(mode, id)).join('');
  fillForm(mode, { ...S.info.defaults[mode], ...(store(`clipper.form.${mode}`) || {}) });
  $$(`#view-${mode} .opts [data-k]`).forEach((el) => el.addEventListener('change', () => store(`clipper.form.${mode}`, readForm(mode))));
}

function fillForm(mode, vals) {
  $$(`#view-${mode} .opts [data-k]`).forEach((el) => {
    const v = vals[el.dataset.k];
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.dataset.k === 'quality' && v === 'best' && mode === 'live') el.value = '';
    else if (el.dataset.k === 'streamer' && v === 'auto') el.value = '';
    else el.value = v ?? '';
  });
}

function readForm(mode) {
  const out = {};
  $$(`#view-${mode} .opts [data-k]`).forEach((el) => {
    const k = el.dataset.k;
    if (el.type === 'checkbox') out[k] = el.checked;
    else if (el.type === 'number') { if (el.value !== '') out[k] = Number(el.value); }
    else if (el.value.trim() !== '') out[k] = el.value.trim();
  });
  return out;
}

// --------------------------------------------------------------------------- tabs & sidebar
function setTab(tab) {
  S.tab = tab;
  $$('.nav .nav-item').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${tab}`));
  if (tab === 'setup') loadSetup();
  if (tab === 'captions') loadCaptions();
  store('clipper.tab', tab);
  renderSidebar();
  renderAll();
}

function jobForSession(name) {
  return Object.values(S.jobs).filter((j) => j.mode !== 'captions' && j.sessions.includes(name))
    .sort((a, b) => b.started - a.started)[0] || null;
}

function captionJobFor(name) {
  return Object.values(S.jobs).find((j) => j.running && j.mode === 'captions' && j.sessions.includes(name)) || null;
}

function selectSession(name, mode) {
  S.sel[mode].session = name;
  const j = jobForSession(name);
  S.sel[mode].job = j ? j.id : null;
  setTab(mode);
  loadSession(name);
}

function selectJob(id) {
  const j = S.jobs[id];
  if (!j) return;
  S.sel[j.mode].job = id;
  S.sel[j.mode].session = j.sessions[0] || null;
  if (j.sessions[0]) loadSession(j.sessions[0]);
  setTab(j.mode);
}

function renderSidebar() {
  const list = $('#runList');
  const running = Object.values(S.jobs).filter((j) => j.running).sort((a, b) => b.started - a.started);
  const cur = S.sel[S.tab] || {};
  for (const m of ['vod', 'live', 'captions']) $(`#dot-${m}`).hidden = !running.some((j) => j.mode === m);
  let html = '';
  if (running.length) {
    html += running.map((j) => {
      const live = j.mode === 'live' && j.state === 'live';
      const badge = live ? '<span class="badge live">LIVE</span>' : '<span class="badge run-on">RUN</span>';
      const meta = j.mode === 'live' ? liveStateLabel(j.state) : (j.stage || 'running…');
      return `<button class="run${cur.job === j.id ? ' active' : ''}" data-job="${j.id}">${badge}
        <span class="r-title">${esc(j.title)}</span><span class="r-meta">${esc(meta)}</span></button>`;
    }).join('');
  }
  const runningSessions = new Set(running.flatMap((j) => j.sessions));
  const saved = S.sessions.filter((s) => !runningSessions.has(s.name));
  html += saved.map((s) => {
    const badge = s.mode === 'live' ? '<span class="badge">LIVE</span>' : '<span class="badge">VOD</span>';
    const who = s.streamer || s.name;
    const what = s.mode === 'live' ? `live ${s.name.split('_').slice(-2).join(' ').replace(/^(\d{4})(\d{2})(\d{2}) (\d{2})(\d{2})\d{2}$/, '$2/$3 $4:$5')}` : s.name;
    const clips = s.clips ? `${s.clips} clip${s.clips > 1 ? 's' : ''}` : `${s.moments} moment${s.moments === 1 ? '' : 's'}`;
    return `<button class="run${cur.session === s.name && !(cur.job && S.jobs[cur.job]?.running) ? ' active' : ''}" data-session="${esc(s.name)}" data-mode="${s.mode}">${badge}
      <span class="r-title">${esc(who)}</span><span class="r-meta">${esc(what)} · ${clips} · ${ago(s.mtime)}</span></button>`;
  }).join('');
  list.innerHTML = html || '<div class="empty">Nothing yet. Runs you start show up here.</div>';
}

// --------------------------------------------------------------------------- jobs
async function startJob(mode, body) {
  try {
    const j = await api('/api/jobs', { method: 'POST', body: { mode, ...body } });
    S.jobs[j.id] = j;
    S.sel[mode] = { job: j.id, session: null };
    renderSidebar();
    renderAll();
    pollSoon();
    return j;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

function jc(id) {
  return (S.jc[id] ||= { log: [], events: [], logNext: 0, evNext: 0,
    d: { rows: [], chat: [], feed: [], clips: {}, status: null, state: 'starting', t0: null, stage: '', progress: null, newChat: [], newFeed: [], newLog: [] } });
}

async function pollJob(id) {
  const c = jc(id);
  const s = await api(`/api/jobs/${id}?log=${c.logNext}&ev=${c.evNext}`);
  const wasRunning = S.jobs[id]?.running;
  S.jobs[id] = s;
  c.log.push(...s.log); c.d.newLog.push(...s.log);
  c.logNext = s.log_next; c.evNext = s.ev_next;
  let reload = false;
  for (const ev of s.events) {
    c.events.push(ev);
    const d = c.d;
    switch (ev.type) {
      case 'window': d.rows.push(ev.row); break;
      case 'chat': d.chat.push(...ev.messages); d.newChat.push(...ev.messages); break;
      case 'skipped': { const f = { kind: 'skip', ...ev }; d.feed.push(f); d.newFeed.push(f); break; }
      case 'clip': {
        d.clips[ev.rank] = ev.status;
        if (ev.total) d.progress = { n: ev.n - (ev.status === 'working' ? 1 : 0), total: ev.total, working: ev.status === 'working' ? ev.rank : null };
        if (s.mode === 'live' && ev.status === 'working') { const f = { kind: 'hit', rank: ev.rank, time: ev.time }; d.feed.push(f); d.newFeed.push(f); }
        reload = true; break;
      }
      case 'status': d.status = ev; break;
      case 'state': d.state = ev.state; if (ev.t0) d.t0 = ev.t0; break;
      case 'stage': d.stage = ev.text; break;
      case 'report': reload = true; break;
      case 'session': {
        // A VOD run shows its latest VOD; a live run keeps showing the live session even
        // when its VOD follow-up starts (that one gets a button instead).
        if (S.sel[s.mode].job === id && (s.mode !== 'live' || ev.mode === 'live')) S.sel[s.mode].session = ev.name;
        reload = true; break;
      }
    }
  }
  if (wasRunning && !s.running) {
    reload = true;
    refreshSessions();
    if (s.returncode === 0) toast(`${s.title}: finished`);
    else if (!s.stop_requested) toast(`${s.title}: stopped with an error. See the log.`, true);
  }
  if (reload) {
    for (const name of s.sessions) if (Object.values(S.sel).some((x) => x.session === name)) scheduleLoad(name);
  }
  if (c.d.chat.length > 400) c.d.chat.splice(0, c.d.chat.length - 300);
}

let pollTimer = null;
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(tick, 150); }

async function tick() {
  let anyRunning = false;
  try {
    const jobs = await api('/api/jobs');
    for (const j of jobs) S.jobs[j.id] = { ...(S.jobs[j.id] || {}), ...j };
    const ids = new Set([S.sel.vod.job, S.sel.live.job].filter(Boolean));
    for (const j of jobs) if (j.running) ids.add(j.id);
    for (const id of ids) if (S.jobs[id]) await pollJob(id);
    anyRunning = jobs.some((j) => j.running);
    if (S.offline) { S.offline = false; toast('Reconnected to the dashboard server.'); }
  } catch (e) {
    if (!S.offline && e instanceof TypeError) { S.offline = true; toast('Lost connection to the dashboard. Is the clipper ui window still open?', true); }
  }
  renderSidebar();
  renderAll();
  pollTimer = setTimeout(tick, anyRunning ? 1000 : 3000);
}

async function refreshSessions() {
  try { S.sessions = await api('/api/sessions'); renderSidebar(); } catch { /* offline */ }
}

const loadTimers = {};
function scheduleLoad(name) {
  clearTimeout(loadTimers[name]);
  loadTimers[name] = setTimeout(() => loadSession(name), 400);
}
async function loadSession(name) {
  try {
    S.sess[name] = await api(`/api/session?name=${encodeURIComponent(name)}`);
    renderAll();
  } catch (e) {
    if (!/no such session/.test(e.message)) toast(e.message, true);
  }
}

function liveStateLabel(st) {
  return ({ starting: 'Starting…', waiting: 'Waiting for the stream to go live', live: 'Live: watching chat',
    reconnecting: 'Stream dropped, checking if it comes back', finishing: 'Finishing clips', vod: 'Clipping the VOD', done: 'Done' })[st] || st;
}

// --------------------------------------------------------------------------- job panel
function renderJob(mode) {
  const box = $(`#${mode}Job`);
  const id = S.sel[mode].job;
  const j = id && S.jobs[id];
  if (!j) { box.innerHTML = ''; box.dataset.job = ''; return; }
  const c = jc(id);
  if (box.dataset.job !== id) {
    box.dataset.job = id;
    box.innerHTML = `<div class="card job">
      <div class="job-head"><h3>${esc(j.title)}</h3><span class="job-state"></span><span class="spacer"></span>
        <button class="btn small" data-act="stop">Stop</button>
        <button class="btn small danger" data-act="kill" title="Ends it right away. Clips being made are lost.">Force stop</button></div>
      ${mode !== 'live' ? '<div class="progress" hidden><div></div></div>' : `
      <div class="stats">
        <div class="stat"><div class="v" data-s="elapsed">–</div><div class="l">Watching for</div></div>
        <div class="stat"><div class="v" data-s="rate">–</div><div class="l">Chat messages / min</div></div>
        <div class="stat"><div class="v" data-s="clips">0</div><div class="l">Clips</div></div>
        <div class="stat"><div class="v" data-s="skipped">0</div><div class="l">Spikes skipped</div></div>
      </div>
      <div class="live-cols">
        <div><div class="feed-title">Chat (after filtering)</div><div class="feed" data-f="chat"></div></div>
        <div><div class="feed-title">Spikes</div><div class="feed" data-f="feed"><div class="empty">Spikes show up here. Clips are cut about a minute after the moment.</div></div></div>
      </div>`}
      <div class="error-box" hidden></div>
      <div class="cmd" title="The same run from a terminal"></div>
      <details class="log-wrap"><summary>Log</summary><pre class="log"></pre></details>
    </div>`;
    $('[data-act=stop]', box).onclick = () => api(`/api/jobs/${id}/stop`, { method: 'POST' }).then(() => { toast('Stopping… finishing any clip in progress.'); pollSoon(); }).catch((e) => toast(e.message, true));
    $('[data-act=kill]', box).onclick = () => {
      if (confirm('Force stop? Anything not finished yet is lost.')) api(`/api/jobs/${id}/kill`, { method: 'POST' }).then(pollSoon).catch((e) => toast(e.message, true));
    };
    c.d.newLog = [...c.log];
    c.d.newChat = c.d.chat.slice(-150);
    c.d.newFeed = [...c.d.feed];
  }
  const d = c.d;
  $('.cmd', box).textContent = j.command;

  // state line
  let st;
  if (j.running) {
    if (mode === 'live') {
      st = (d.state === 'live' ? '<span class="live-dot"></span>' : '<span class="spin"></span>') + esc(liveStateLabel(d.state));
      if (j.stop_requested && d.state !== 'finishing') st = '<span class="spin"></span>Stopping…';
    } else {
      const p = d.progress;
      const verb = mode === 'captions' ? ['Adding captions to clip', 'Captioned'] : ['Cutting clip', 'Cut'];
      const txt = p ? (p.working ? `${verb[0]} ${p.n + 1} of ${p.total}` : `${verb[1]} ${p.n} of ${p.total}`) : (d.stage || (j.argv.includes('--dry-run') ? 'Ranking moments' : 'Working'));
      st = `<span class="spin"></span>${esc(j.stop_requested ? 'Stopping after this clip…' : txt)}`;
    }
  } else if (j.returncode === 0) {
    st = `<span class="ok-dot"></span>Finished`;
  } else if (j.stop_requested) {
    st = `<span class="ok-dot"></span>Stopped`;
  } else {
    st = `<span class="err-dot"></span>Failed (exit ${j.returncode})`;
  }
  $('.job-state', box).innerHTML = st;
  $('[data-act=stop]', box).hidden = !j.running || j.stop_requested;
  $('[data-act=kill]', box).hidden = !j.running;

  // error summary
  const eb = $('.error-box', box);
  if (!j.running && j.returncode && !j.stop_requested) {
    const i = c.log.findIndex((l) => /^error:/.test(l));
    const lines = i >= 0 ? c.log.slice(i, i + 12) : c.log.slice(-8);
    eb.textContent = lines.join('\n');
    eb.hidden = false;
  } else eb.hidden = true;

  if (mode !== 'live') {
    const pr = $('.progress', box);
    if (d.progress && d.progress.total) {
      pr.hidden = false;
      $('div', pr).style.width = `${(100 * (d.progress.n + (d.progress.working ? 0.4 : 0))) / d.progress.total}%`;
    } else pr.hidden = true;
  } else {
    const t0 = d.t0;
    const end = j.running ? Date.now() / 1000 : (j.ended || Date.now() / 1000);
    $('[data-s=elapsed]', box).textContent = t0 ? hms(end - t0, true) : '–';
    const last = d.rows.slice(-6);
    $('[data-s=rate]', box).textContent = last.length ? Math.round(last.reduce((a, r) => a + r[5], 0) * (6 / last.length)) : '–';
    const rep = S.sess[S.sel.live.session]?.report;
    $('[data-s=clips]', box).textContent = rep ? rep.clips.length : Object.keys(d.clips).length;
    $('[data-s=skipped]', box).textContent = d.feed.filter((f) => f.kind === 'skip').length;

    const chat = $('[data-f=chat]', box);
    if (d.newChat.length) {
      const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
      chat.insertAdjacentHTML('beforeend', d.newChat.map(([u, b]) => `<div class="msg"><b>${esc(u)}</b> ${esc(b)}</div>`).join(''));
      while (chat.children.length > 150) chat.firstChild.remove();
      if (atBottom) chat.scrollTop = chat.scrollHeight;
      d.newChat = [];
    }
    const feed = $('[data-f=feed]', box);
    if (d.newFeed.length) {
      if (feed.querySelector('.empty')) feed.innerHTML = '';
      for (const f of d.newFeed) {
        const t = f.clock ? f.clock.replace(/h|m/g, ':').replace('s', '') : new Date((f.time || 0) * 1000).toLocaleTimeString();
        const html = f.kind === 'skip'
          ? `<div class="ev skip"><span class="t">${esc(t)}</span>Skipped (score ${f.score}): ${esc(f.reason)}${f.top?.length ? ` · <i>${esc(f.top.join(', '))}</i>` : ''}</div>`
          : `<div class="ev hit" data-rank="${f.rank}"><span class="t">${esc(t)}</span>Clip ${f.rank}: <span class="hit-txt">cutting…</span></div>`;
        feed.insertAdjacentHTML('afterbegin', html);
      }
      d.newFeed = [];
    }
    // fill in clip details from the report
    const recs = Object.fromEntries((rep?.clips || []).map((r) => [r.rank, r]));
    $$('.ev.hit', feed).forEach((el) => {
      const r = recs[el.dataset.rank], status = d.clips[el.dataset.rank];
      if (!r) return;
      const top = r.top_messages.slice(0, 3).map((m) => m.text).join(', ');
      const what = status === 'done' ? 'ready' : status === 'failed' ? 'failed' : 'cutting…';
      el.querySelector('.hit-txt').textContent = `score ${Math.round(r.peak_score)} (${r.ratio}× normal), ${what} · ${top}`;
    });
  }

  if (d.newLog.length) {
    const pre = $('pre.log', box);
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent += (pre.textContent ? '\n' : '') + d.newLog.join('\n');
    if (pre.textContent.length > 400000) pre.textContent = pre.textContent.slice(-300000);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    d.newLog = [];
  }
}

// --------------------------------------------------------------------------- session panel
function renderSession(mode) {
  const box = $(`#${mode}Session`);
  const name = S.sel[mode].session;
  const data = name && S.sess[name];
  if (!data || !data.report) {
    box.innerHTML = ''; box.dataset.name = '';
    return;
  }
  const r = data.report;
  const job = S.sel[mode].job ? S.jobs[S.sel[mode].job] : null;
  const capJob = captionJobFor(name);
  const busy = (job && job.running) || !!capJob;
  if (box.dataset.name !== name) {
    box.dataset.name = name;
    box.innerHTML = `<div class="card session">
      <div class="sess-head"><div><h2></h2><div class="meta"></div></div><div class="sess-actions"></div></div>
      <div class="chart-wrap"><canvas></canvas><div class="tip"></div></div>
      <div class="legend"><span><i style="background:var(--chart-bar)"></i>chat score</span><span><i style="background:var(--accent)"></i>spike</span>
        <span><i style="background:var(--warn);height:2px"></i>spike threshold</span><span><i style="background:var(--accent-soft);border:1px solid var(--accent)"></i>clip</span>
        <span><i style="background:var(--chart-mute);border:1px solid var(--line)"></i>muted (raid, gift bomb, outro)</span></div>
      <div class="moments-head"><h3></h3><span class="spacer"></span><span class="hint pick-hint"></span><button class="btn primary small" data-act="cutsel" hidden></button></div>
      <div class="moments"></div>
    </div>`;
    setupChartHover(box, mode);
    $('[data-act=cutsel]', box).onclick = () => cutSelected(name);
  }
  // header
  const live = data.mode === 'live';
  const liveRows = live && job && job.sessions[0] === name ? jc(job.id).d.rows : null;
  $('h2', box).textContent = live ? `${r.streamer} · live session` : `${r.streamer || 'VOD'} · ${r.title || r.vod_id}`;
  const muted = r.chat.muted_periods.length;
  const win = data.activity?.window || r.params.window;
  const runningLive = busy && liveRows && liveRows.length;
  $('.meta', box).textContent = [
    runningLive ? `${hms(liveRows[liveRows.length - 1][0] + win)} watched` : `${hms(r.vod_length)} ${live ? 'watched' : 'long'}`,
    runningLive ? `${liveRows.reduce((a, x) => a + x[5], 0).toLocaleString()} chat messages after filtering`
      : `${r.chat.total_messages.toLocaleString()} chat messages (${r.chat.kept_messages.toLocaleString()} after filtering)`,
    muted ? `${muted} muted period${muted > 1 ? 's' : ''}` : null,
    `sensitivity ${r.params.multiplier}${r.params.method === 'zscore' ? ` (z ${r.params.z})` : ''}`,
  ].filter(Boolean).join(' · ');
  const acts = [`<button class="btn small" data-act="open">Open folder</button>`];
  if (r.clips.some((c) => c.file && data.files[c.file]) && !busy) acts.push(`<button class="btn small" data-act="captions">Add captions</button>`);
  if (!live) acts.push(`<a class="btn small" href="https://www.twitch.tv/videos/${esc(r.vod_id)}" target="_blank" rel="noopener">Watch VOD on Twitch ↗</a>`);
  const follow = live && job && job.sessions.find((s) => s !== name);
  if (follow) acts.push(`<button class="btn small" data-act="follow">VOD follow-up: ${esc(follow)} →</button>`);
  const actsHTML = acts.join('');
  const sa = $('.sess-actions', box);
  if (sa.dataset.html !== actsHTML) {
    sa.innerHTML = actsHTML; sa.dataset.html = actsHTML;
    $('[data-act=open]', sa).onclick = () => api('/api/open', { method: 'POST', body: { name } }).catch((e) => toast(e.message, true));
    const fb = $('[data-act=follow]', sa);
    if (fb) fb.onclick = () => selectSession(follow, 'vod');
    const cb = $('[data-act=captions]', sa);
    if (cb) cb.onclick = () => { CAP.run = name; setTab('captions'); };
  }

  // chart
  const rows = liveRows && liveRows.length ? liveRows : (data.activity?.rows || []);
  const end = rows.length ? rows[rows.length - 1][0] + win : 0;
  // while live, show at least 10 minutes so the bars grow from the left instead of filling the chart
  const chartData = { rows, win, moments: r.clips, length: live ? (busy ? Math.max(600, end) : end) : r.vod_length,
    empty: busy && live ? 'Chat activity appears here once the stream is live.'
      : (live ? 'No chat activity saved for this session.' : 'Made before the dashboard existed. Preview it again to see chat activity.') };
  box._chart = chartData;
  drawChart($('canvas', box), chartData);

  // moments
  const clips = r.clips;
  const unpicked = (S.unpicked[name] ||= new Set());
  const clipStatus = { ...(job ? jc(job.id).d.clips : {}) };
  if (capJob) for (const [rank, st] of Object.entries(jc(capJob.id).d.clips)) clipStatus[rank] = st === 'working' ? 'captioning' : clipStatus[rank];
  $('.moments-head h3', box).textContent = clips.length
    ? `${clips.length} moment${clips.length > 1 ? 's' : ''}${live ? ' clipped' : ''}`
    : (live ? 'No clips yet' : 'No moments found. Try a lower sensitivity or minimum spike score.');
  const cutBtn = $('[data-act=cutsel]', box);
  const missing = clips.filter((c) => !c.file && !c.clipped_live);
  const picked = clips.filter((c) => !unpicked.has(c.rank) && !c.clipped_live);
  if (!live && clips.length && !busy) {
    cutBtn.hidden = false;
    cutBtn.disabled = !picked.length;
    cutBtn.textContent = missing.length === clips.length ? `Cut ${picked.length} selected clip${picked.length === 1 ? '' : 's'}` : `Cut ${picked.length} selected (re-cuts existing)`;
    $('.pick-hint', box).textContent = 'Untick moments you don\'t want, then cut.';
  } else {
    cutBtn.hidden = true;
    $('.pick-hint', box).textContent = '';
  }
  const grid = $('.moments', box);
  const seen = new Set();
  const autoUn = (S.autoUnpicked ||= new Set());
  clips.forEach((c, i) => {
    const mtime = c.file ? data.files[c.file] : null;
    // a deleted clip starts unticked, so "Cut selected" doesn't bring it back (tick it to re-cut)
    if (c.deleted && !autoUn.has(`${name}#${c.rank}`)) { autoUn.add(`${name}#${c.rank}`); unpicked.add(c.rank); }
    const status = clipStatus[c.rank];
    const sig = JSON.stringify([c.file, mtime, status, c.error, c.clipped_live, c.deleted, c.not_selected, unpicked.has(c.rank), c.peak_time, live, c.facecam, !!busy,
      c.captions, c.captions_error, (data.subs || []).includes(c.captions?.srt)]);
    const key = `m-${c.rank}`;
    seen.add(key);
    let el = grid.querySelector(`[data-key="${key}"]`);
    if (el && el.dataset.sig === sig) { if (grid.children[i] !== el) grid.insertBefore(el, grid.children[i] || null); return; }
    const html = momentHTML(name, c, mtime, status, live, unpicked.has(c.rank), busy, data.subs || []);
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    const fresh = tmp.firstElementChild;
    fresh.dataset.key = key; fresh.dataset.sig = sig;
    if (el) el.replaceWith(fresh); else grid.insertBefore(fresh, grid.children[i] || null);
    const cb = fresh.querySelector('input[type=checkbox]');
    if (cb) cb.onchange = () => { cb.checked ? unpicked.delete(c.rank) : unpicked.add(c.rank); renderAll(); };
    const del = fresh.querySelector('[data-act=delclip]');
    if (del) del.onclick = () => deleteClip(name, c);
  });
  [...grid.children].forEach((el) => { if (!seen.has(el.dataset.key)) el.remove(); });
}

function momentHTML(session, c, mtime, status, live, unpicked, busy, subs) {
  let media;
  if (status === 'captioning') {
    media = `<div class="ph"><span class="spin"></span>Adding captions…</div>`;
  } else if (c.file && mtime) {
    media = `<video src="/media/${encodeURIComponent(session)}/${encodeURIComponent(c.file)}?v=${mtime}" controls preload="metadata" playsinline></video>`;
  } else if (busy && (status === 'working' || (live && c.file && !c.error && status !== 'failed'))) {
    media = `<div class="ph"><span class="spin"></span>Cutting…</div>`;
  } else if (c.error) {
    media = `<div class="ph err">Failed<br>${esc(c.error)}</div>`;
  } else if (c.clipped_live) {
    media = `<div class="ph">Already clipped live<br>${esc(c.clipped_live)}</div>`;
  } else if (c.deleted) {
    media = `<div class="ph">Deleted</div>`;
  } else {
    media = `<div class="ph">Not cut yet</div>`;
  }
  const when = live && c.clock_time ? c.clock_time.slice(11) : c.peak_time_hms;
  const top = (c.top_messages || []).slice(0, 5).map((m) => `<span class="chip" title="${esc(m.text)}">${esc(m.text)}<em>×${m.count}</em></span>`).join('');
  const links = [];
  if (c.vod_link) links.push(`<a href="${esc(c.vod_link)}" target="_blank" rel="noopener">Watch on Twitch ↗</a>`);
  links.push(`<span>${Math.round(c.duration)}s clip</span>`);
  if (c.layout === 'stacked') links.push(`<span>facecam ${c.facecam ?? ''}</span>`);
  else if (c.layout === 'center' && c.facecam === null) links.push('<span>no face found, center crop</span>');
  if (c.captions && c.file && mtime) {
    links.push(c.captions.words ? '<span class="cc" title="Captions burned in">CC</span>' : '<span>no speech, no captions</span>');
    if (c.captions.srt && subs.includes(c.captions.srt)) links.push(`<a href="/media/${encodeURIComponent(session)}/${encodeURIComponent(c.captions.srt)}" download>subtitles</a>`);
  } else if (c.captions_error && c.file) {
    links.push(`<span class="cc-err" title="${esc(c.captions_error)}">captions failed</span>`);
  }
  if (c.file && mtime && !busy) links.push('<button class="del" data-act="delclip" title="Delete this video from your computer">Delete</button>');
  const pick = !live && !c.clipped_live
    ? `<label class="pick"><input type="checkbox"${unpicked ? '' : ' checked'}>cut</label>` : '';
  return `<div class="moment${unpicked && !live ? ' unpicked' : ''}" id="moment-${esc(session)}-${c.rank}">
    <div class="media">${media}</div>
    <div class="m-body">
      <div class="m-top"><span class="rank">#${c.rank}</span><span class="m-time">${esc(when)}</span>${pick}</div>
      <div class="m-score">score ${Math.round(c.peak_score)} · ${c.ratio}× normal · ${c.peak_unique_chatters} chatters</div>
      <div class="chips">${top}</div>
      <div class="m-links">${links.join('')}</div>
    </div></div>`;
}

async function deleteClip(name, c) {
  if (!confirm(`Delete clip #${c.rank}?\n\n${c.file} will be permanently deleted from your computer `
    + '(not moved to the Recycle Bin), with its captions and subtitles.')) return;
  try {
    await api('/api/clips/delete', { method: 'POST', body: { session: name, rank: c.rank } });
    (S.unpicked[name] ||= new Set()).add(c.rank);
    toast(`Deleted clip #${c.rank}.`);
    await loadSession(name);
    refreshSessions();
  } catch (e) { toast(e.message, true); }
}

async function cutSelected(name) {
  const data = S.sess[name];
  if (!data) return;
  const r = data.report, p = r.params;
  const un = S.unpicked[name] || new Set();
  const ranks = r.clips.filter((c) => !un.has(c.rank) && !c.clipped_live).map((c) => c.rank);
  if (!ranks.length) return;
  const form = readForm('vod');
  // Detection settings come from this preview, so the ranks match. Video settings from the form.
  const options = {
    window: p.window, method: p.method, multiplier: p.multiplier, z: p.z, baseline: p.baseline_span,
    min_score: p.min_score, merge_gap: p.merge_gap, pre_roll: p.pre_roll, post_roll: p.post_roll,
    min_len: p.min_len, max_len: p.max_len, max_clips: p.max_clips, rank_by: p.rank_by,
    layout: form.layout, quality: form.quality, streamer: form.streamer, downloader: form.downloader,
    per_user_cap: form.per_user_cap, keep_raw: form.keep_raw, captions: form.captions,
    only: ranks.length === r.clips.length ? undefined : ranks.join(','),
  };
  const j = await startJob('vod', { inputs: [r.vod_id], options });
  if (j) S.sel.vod.session = name;
}

// --------------------------------------------------------------------------- chart
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

function drawChart(canvas, cd) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (!W) return;
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const { rows, win, moments } = cd;
  const padL = 34, padR = 8, padT = 16, padB = 22;
  const cw = W - padL - padR, ch = H - padT - padB;
  const total = Math.max(cd.length || 0, rows.length ? rows[rows.length - 1][0] + win : 0, 60);
  let maxY = 10;
  for (const r of rows) { if (r[1] > maxY) maxY = r[1]; if (r[2] > maxY && r[2] < 1e6) maxY = r[2]; }
  maxY *= 1.1;
  const x = (t) => padL + (t / total) * cw;
  const y = (v) => padT + ch - (Math.min(v, maxY) / maxY) * ch;
  const C = { bar: cssVar('--chart-bar'), acc: cssVar('--accent'), soft: cssVar('--accent-soft'), mute: cssVar('--chart-mute'),
    warn: cssVar('--warn'), line: cssVar('--line'), faint: cssVar('--faint'), text: cssVar('--muted') };
  ctx.font = '11px Segoe UI, system-ui, sans-serif';

  // grid + y labels
  ctx.strokeStyle = C.line; ctx.fillStyle = C.faint; ctx.lineWidth = 1;
  const ystep = niceStep(maxY / 4);
  for (let v = 0; v <= maxY; v += ystep) {
    const yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(String(v), padL - 6, yy);
  }
  // muted windows
  ctx.fillStyle = C.mute;
  for (const r of rows) if (r[4]) ctx.fillRect(x(r[0]), padT, Math.max(1, x(r[0] + win) - x(r[0])), ch);
  // clip bands
  for (const m of moments) {
    const a = x(m.clip_start), b = x(m.clip_end);
    ctx.fillStyle = C.soft; ctx.fillRect(a, padT, Math.max(2, b - a), ch);
  }
  // bars (aggregated per pixel column when dense)
  const colW = Math.max(1, x(win) - x(0));
  if (colW >= 2) {
    for (const r of rows) {
      ctx.fillStyle = r[3] ? C.acc : C.bar;
      const bx = x(r[0]), bw = Math.max(1, colW - (colW > 4 ? 1 : 0));
      ctx.fillRect(bx, y(r[1]), bw, padT + ch - y(r[1]));
    }
  } else {
    const cols = Math.ceil(cw);
    const mx = new Float32Array(cols), sp = new Uint8Array(cols);
    for (const r of rows) {
      const ci = Math.min(cols - 1, Math.floor((r[0] / total) * cw));
      if (r[1] > mx[ci]) mx[ci] = r[1];
      if (r[3]) sp[ci] = 1;
    }
    for (let i = 0; i < cols; i++) {
      if (!mx[i]) continue;
      ctx.fillStyle = sp[i] ? C.acc : C.bar;
      ctx.fillRect(padL + i, y(mx[i]), 1, padT + ch - y(mx[i]));
    }
  }
  // threshold
  ctx.strokeStyle = C.warn; ctx.lineWidth = 1.25; ctx.setLineDash([4, 3]); ctx.beginPath();
  let started = false;
  const stepRows = Math.max(1, Math.floor(rows.length / cw));
  for (let i = 0; i < rows.length; i += stepRows) {
    const r = rows[i];
    if (!(r[2] > 0 && r[2] < 1e6)) { started = false; continue; }
    const px = x(r[0] + win / 2), py = y(r[2]);
    if (started) ctx.lineTo(px, py); else { ctx.moveTo(px, py); started = true; }
  }
  ctx.stroke(); ctx.setLineDash([]);
  // clip labels
  ctx.fillStyle = C.acc; ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.font = '600 11px Segoe UI, system-ui, sans-serif';
  let lastLabel = -1e9;
  for (const m of [...moments].sort((a, b) => a.clip_start - b.clip_start)) {
    const cx = x((m.clip_start + m.clip_end) / 2);
    if (cx - lastLabel < 16) continue;
    ctx.fillText(`#${m.rank}`, cx, 1); lastLabel = cx;
  }
  // x labels
  ctx.font = '11px Segoe UI, system-ui, sans-serif'; ctx.fillStyle = C.faint; ctx.textBaseline = 'top';
  const tstep = [30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400].find((s) => total / s <= 9) || 28800;
  for (let t = 0; t <= total; t += tstep) {
    ctx.textAlign = t === 0 ? 'left' : 'center';
    ctx.fillText(hms(t, total < 3600), x(t), padT + ch + 6);
  }
  if (!rows.length) {
    ctx.fillStyle = C.text; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(cd.empty || 'No chat activity saved for this run.', padL + cw / 2, padT + ch / 2);
  }
  canvas._g = { padL, cw, total, win, rows, moments };
}

function niceStep(v) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1))));
  for (const m of [1, 2, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function setupChartHover(box, mode) {
  const canvas = $('canvas', box), tip = $('.tip', box);
  const at = (e) => {
    const g = canvas._g;
    if (!g) return null;
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const t = ((px - g.padL) / g.cw) * g.total;
    if (t < 0 || t > g.total) return null;
    const row = g.rows[Math.floor(t / g.win)] && g.rows[Math.floor(t / g.win)][0] === Math.floor(t / g.win) * g.win
      ? g.rows[Math.floor(t / g.win)] : g.rows.find((r) => r[0] <= t && t < r[0] + g.win);
    const m = g.moments.find((c) => c.clip_start <= t && t <= c.clip_end);
    return { t, row, m, px, rect };
  };
  canvas.addEventListener('mousemove', (e) => {
    const h = at(e);
    if (!h) { tip.style.display = 'none'; return; }
    const parts = [`<b>${hms(h.t)}</b>`];
    if (h.row) {
      parts.push(`score ${h.row[1]} · threshold ${h.row[2] < 1e6 ? h.row[2] : '–'} · ${h.row[5]} msgs`);
      if (h.row[4]) parts.push(`muted: ${esc(h.row[4])}`);
    }
    if (h.m) parts.push(`<span style="color:var(--accent-2)">clip #${h.m.rank}, click to jump</span>`);
    tip.innerHTML = parts.join('<br>');
    tip.style.display = 'block';
    const left = Math.min(h.px + 12, h.rect.width - tip.offsetWidth - 4);
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = '8px';
    canvas.style.cursor = h.m ? 'pointer' : 'crosshair';
  });
  canvas.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  canvas.addEventListener('click', (e) => {
    const h = at(e);
    if (!h || !h.m) return;
    const el = document.getElementById(`moment-${S.sel[mode].session}-${h.m.rank}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('flash');
      setTimeout(() => el.classList.remove('flash'), 1400);
    }
  });
}

window.addEventListener('resize', () => {
  for (const mode of ['vod', 'live']) {
    const box = $(`#${mode}Session`);
    if (box._chart) drawChart($('canvas', box), box._chart);
  }
  if (S.tab === 'setup') fcDraw();
});

function renderAll() {
  if (!S.info) return;
  if (S.tab === 'vod' || S.tab === 'live') {
    renderJob(S.tab);
    renderSession(S.tab);
  } else if (S.tab === 'captions') {
    renderJob('captions');
    renderCapRuns();
  }
}

// --------------------------------------------------------------------------- setup: tools
const TOOL_LABELS = { twitchdownloader: 'TwitchDownloaderCLI', ffmpeg: 'ffmpeg', streamlink: 'streamlink', 'yt-dlp': 'yt-dlp' };
const TOOL_USE = { twitchdownloader: 'VOD chat and video', ffmpeg: 'Cutting and rendering clips', streamlink: 'Live mode video', 'yt-dlp': 'Finding VODs after a stream; backup VOD downloader' };

function renderTools() {
  const i = S.info;
  const rows = Object.entries(i.tools).map(([k, p]) => `<div class="tool">
    <span class="ico ${p ? 'ok' : 'no'}">${p ? '✓' : '!'}</span>
    <div><b>${TOOL_LABELS[k]}</b><div class="hint">${TOOL_USE[k]}</div></div>
    ${p ? `<div class="p">${esc(p)}</div>` : `<pre>${esc(i.install[k])}</pre>`}</div>`);
  rows.push(`<div class="tool"><span class="ico ${i.opencv ? 'ok' : 'no'}">${i.opencv ? '✓' : '!'}</span>
    <div><b>OpenCV</b><div class="hint">Picks the right facecam box per clip</div></div>
    ${i.opencv ? '<div class="p">installed</div>' : '<pre>pip install "opencv-python-headless<5"   (only needed with several facecam positions)</pre>'}</div>`);
  rows.push(`<div class="tool"><span class="ico ok">${i.oauth ? '✓' : '–'}</span>
    <div><b>Twitch login token</b><div class="hint">Only for sub-only VODs</div></div>
    <div class="p">${i.oauth ? 'TWITCH_OAUTH is set' : 'Not set. Not needed for public VODs and streams.'}</div></div>`);
  rows.push(`<div class="tool"><span class="ico ok">📁</span><div><b>Clips folder</b></div><div class="p">${esc(i.out_dir)}</div></div>`);
  $('#toolsList').innerHTML = rows.join('');
  const missing = Object.entries(i.tools).filter(([k, p]) => !p && k !== 'yt-dlp').map(([k]) => TOOL_LABELS[k]);
  const pill = $('#toolsPill');
  pill.className = 'pill' + (missing.length ? ' bad' : '');
  pill.innerHTML = `<span class="dot"></span>${missing.length ? `Missing: ${esc(missing.join(', '))}` : 'All tools found'}`;
}

// --------------------------------------------------------------------------- setup: facecam picker
const FC = { img: null, natW: 1920, natH: 1080, boxes: [], sel: -1, drag: null, src: 'vod' };
const FC_COLORS = ['#7c5cff', '#3ecf8e', '#f5b84b', '#ff6b6b', '#4cc3ff'];

function fcGeom() {
  const c = $('#fcCanvas');
  const W = c.clientWidth, H = c.clientHeight;
  const sc = Math.min(W / FC.natW, H / FC.natH);
  return { W, H, sc, ox: (W - FC.natW * sc) / 2, oy: (H - FC.natH * sc) / 2 };
}

function fcDraw() {
  const c = $('#fcCanvas');
  const dpr = window.devicePixelRatio || 1;
  const { W, H, sc, ox, oy } = fcGeom();
  if (!W) return;
  c.width = W * dpr; c.height = H * dpr;
  const ctx = c.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (FC.img) ctx.drawImage(FC.img, ox, oy, FC.natW * sc, FC.natH * sc);
  else if (FC.boxes.length) { ctx.fillStyle = 'rgba(128,128,128,.08)'; ctx.fillRect(ox, oy, FC.natW * sc, FC.natH * sc); }
  $('#fcEmpty').style.display = FC.img || FC.boxes.length ? 'none' : 'grid';
  const all = FC.drag ? [...FC.boxes, FC.drag] : FC.boxes;
  all.forEach((b, i) => {
    const col = FC_COLORS[i % FC_COLORS.length];
    const x = ox + b.x * sc, y = oy + b.y * sc, w = b.w * sc, h = b.h * sc;
    ctx.fillStyle = col + '33'; ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = col; ctx.lineWidth = i === FC.sel ? 3 : 2; ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = col; ctx.fillRect(x, y, 20, 18);
    ctx.fillStyle = '#fff'; ctx.font = '600 12px Segoe UI, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(String(i + 1), x + 10, y + 9);
  });
  fcPreview();
  fcList();
}

function fcPreview() {
  const c = $('#fcPreview'), ctx = c.getContext('2d');
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, c.width, c.height);
  if (!FC.img) {
    ctx.fillStyle = '#666'; ctx.font = '12px Segoe UI, sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('No frame yet', c.width / 2, c.height / 2);
    return;
  }
  const topH = Math.round((c.height * 640) / 1920);
  const b = FC.boxes[FC.sel] || FC.boxes[0];
  // bottom: center crop of the full frame, like the renderer
  const sw = (FC.natH * 1080) / 1280;
  if (b) {
    const a = 1080 / 640;
    let sx = b.x, sy = b.y, sW = b.w, sH = b.h;
    if (b.w / b.h > a) { sW = b.h * a; sx = b.x + (b.w - sW) / 2; } else { sH = b.w / a; sy = b.y + (b.h - sH) / 2; }
    ctx.drawImage(FC.img, sx, sy, sW, sH, 0, 0, c.width, topH);
    ctx.drawImage(FC.img, (FC.natW - sw) / 2, 0, sw, FC.natH, 0, topH, c.width, c.height - topH);
  } else {
    const cw = (FC.natH * 1080) / 1920;
    ctx.drawImage(FC.img, (FC.natW - cw) / 2, 0, cw, FC.natH, 0, 0, c.width, c.height);
  }
}

function fcList() {
  const el = $('#fcBoxes');
  if (!FC.boxes.length) { el.innerHTML = '<div class="hint">No positions yet. Drag on the frame to draw one.</div>'; return; }
  el.innerHTML = FC.boxes.map((b, i) => `<div class="fc-box${i === FC.sel ? ' sel' : ''}" data-i="${i}">
    <span class="n" style="color:${FC_COLORS[i % FC_COLORS.length]}">${i + 1}</span>
    <span class="c">${Math.round(b.x)},${Math.round(b.y)} ${Math.round(b.w)}×${Math.round(b.h)}</span>
    <button title="Delete" data-del="${i}">×</button></div>`).join('');
}

function fcPoint(e) {
  const { sc, ox, oy } = fcGeom();
  const r = $('#fcCanvas').getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(FC.natW, (e.clientX - r.left - ox) / sc)),
    y: Math.max(0, Math.min(FC.natH, (e.clientY - r.top - oy) / sc)),
  };
}

function setupFacecam() {
  const c = $('#fcCanvas');
  let start = null;
  c.addEventListener('pointerdown', (e) => {
    if (!FC.img) { toast('Grab a frame first, then drag around the facecam.'); return; }
    start = fcPoint(e);
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove', (e) => {
    if (!start) return;
    const p = fcPoint(e);
    FC.drag = { x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y) };
    fcDraw();
  });
  c.addEventListener('pointerup', (e) => {
    if (!start) return;
    const p = fcPoint(e);
    const d = FC.drag;
    FC.drag = null;
    if (d && d.w > 12 && d.h > 12) {
      FC.boxes.push(d); FC.sel = FC.boxes.length - 1;
    } else {
      const hit = FC.boxes.findIndex((b) => p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h);
      if (hit >= 0) FC.sel = hit;
    }
    start = null;
    fcDraw();
  });
  $('#fcBoxes').addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]');
    if (del) { FC.boxes.splice(+del.dataset.del, 1); FC.sel = Math.min(FC.sel, FC.boxes.length - 1); fcDraw(); return; }
    const row = e.target.closest('[data-i]');
    if (row) { FC.sel = +row.dataset.i; fcDraw(); }
  });
  $$('#fcSource button').forEach((b) => b.addEventListener('click', () => {
    FC.src = b.dataset.src;
    $$('#fcSource button').forEach((x) => x.classList.toggle('active', x === b));
    $('#fcVodWrap span').textContent = FC.src === 'vod' ? 'VOD link' : 'Channel';
    $('#fcVod').placeholder = FC.src === 'vod' ? 'https://www.twitch.tv/videos/...' : 'channel name (must be live)';
    $('#fcTimeWrap').style.display = FC.src === 'vod' ? '' : 'none';
    $('#fcVod').value = FC.src === 'vod' ? (store('clipper.fc.vod') || '') : ($('#fcStreamer').value || '');
  }));
  $('#fcStreamer').addEventListener('change', () => fcLoadSaved());
  $('#fcGrab').addEventListener('click', fcGrab);
  $('#fcSave').addEventListener('click', fcSave);
}

async function fcLoadSaved() {
  const s = $('#fcStreamer').value.trim().toLowerCase();
  store('clipper.fc.streamer', s);
  if (!s) return;
  try {
    const r = await api(`/api/facecams?streamer=${encodeURIComponent(s)}`);
    if (!r.boxes.length) return;  // nothing saved yet: keep whatever was drawn
    if (!FC.img) { FC.natH = r.boxes[0].ref_height; FC.natW = Math.round((FC.natH * 16) / 9); }
    FC.boxes = r.boxes.map((b) => { const k = FC.natH / b.ref_height; return { x: b.x * k, y: b.y * k, w: b.w * k, h: b.h * k }; });
    FC.sel = FC.boxes.length ? 0 : -1;
    fcDraw();
    if (r.boxes.length) toast(`Loaded ${r.boxes.length} saved position${r.boxes.length > 1 ? 's' : ''} for ${s}.`);
  } catch (e) { toast(e.message, true); }
}

async function fcGrab() {
  const btn = $('#fcGrab');
  const v = $('#fcVod').value.trim();
  if (!v) { toast(FC.src === 'vod' ? 'Paste a VOD link first.' : 'Type the channel name first.', true); return; }
  if (FC.src === 'vod') store('clipper.fc.vod', v);
  btn.disabled = true; const label = btn.textContent; btn.innerHTML = '<span class="spin" style="display:inline-block;vertical-align:-2px"></span> Getting frame…';
  try {
    const body = FC.src === 'vod' ? { source: 'vod', vod: v, time: $('#fcTime').value } : { source: 'live', channel: v };
    const r = await api('/api/frame', { method: 'POST', body });
    const img = new Image();
    img.onload = () => {
      const k = img.naturalHeight / FC.natH;
      FC.boxes = FC.boxes.map((b) => ({ x: b.x * k, y: b.y * k, w: b.w * k, h: b.h * k }));
      FC.img = img; FC.natW = img.naturalWidth; FC.natH = img.naturalHeight;
      fcDraw();
    };
    img.src = r.url;
    if (FC.src === 'live' && !$('#fcStreamer').value) { $('#fcStreamer').value = v.toLowerCase(); fcLoadSaved(); }
    if (FC.src === 'vod' && !$('#fcStreamer').value) toast('Type the streamer\'s channel name in "Streamer" so the positions are saved for them.');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

// The streamers.yaml key a live job reads facecams from: --streamer, or else the channel.
function liveStreamer(job) {
  const i = job.argv.indexOf('--streamer');
  const s = i >= 0 ? job.argv[i + 1] : 'auto';
  return (s && s !== 'auto' ? s : job.argv[1] || '').toLowerCase();
}

async function fcSave() {
  const s = $('#fcStreamer').value.trim().toLowerCase();
  if (!s) { toast('Type the streamer\'s channel name first.', true); $('#fcStreamer').focus(); return; }
  if (!FC.boxes.length && !confirm(`Remove all facecam positions for ${s}?`)) return;
  try {
    const r = await api('/api/facecams', { method: 'POST', body: { streamer: s, ref_height: FC.natH, boxes: FC.boxes } });
    const saved = `Saved ${r.boxes.length} position${r.boxes.length === 1 ? '' : 's'} for ${s}.`;
    const live = Object.values(S.jobs).find((j) => j.running && j.mode === 'live' && liveStreamer(j) === s);
    if (!live) toast(`${saved} Use the "Facecam on top" layout.`);
    else if (live.argv.includes('stacked')) toast(`${saved} The live run for ${s} uses them from its next clip.`);
    else toast(`${saved} The live run for ${s} uses the Center layout, so it won't use them: stop it and start again with "Facecam on top".`, true);
    if (CFG.name === 'streamers.yaml') loadConfig('streamers.yaml');
  } catch (e) { toast(e.message, true); }
}

// --------------------------------------------------------------------------- setup: config editor
const CFG = { name: 'keywords.yaml', original: '' };
async function loadConfig(name) {
  try {
    const r = await api(`/api/config/${name}`);
    CFG.name = name; CFG.original = r.text;
    $('#cfgText').value = r.text;
    $('#cfgPath').textContent = r.path;
    $$('#cfgTabs button').forEach((b) => b.classList.toggle('active', b.dataset.cfg === name));
  } catch (e) { toast(e.message, true); }
}
function setupConfig() {
  $$('#cfgTabs button').forEach((b) => b.addEventListener('click', () => {
    if ($('#cfgText').value !== CFG.original && !confirm('Discard your unsaved changes?')) return;
    loadConfig(b.dataset.cfg);
  }));
  $('#cfgSave').addEventListener('click', async () => {
    try {
      await api(`/api/config/${CFG.name}`, { method: 'PUT', body: { text: $('#cfgText').value } });
      CFG.original = $('#cfgText').value;
      toast(`Saved ${CFG.name}. It applies to the next run.`);
      if (CFG.name === 'streamers.yaml') fcLoadSaved();
    } catch (e) { toast(e.message, true); }
  });
  $('#cfgRevert').addEventListener('click', () => { $('#cfgText').value = CFG.original; });
}

let setupLoaded = false;
function loadSetup() {
  renderTools();
  if (!setupLoaded) {
    setupLoaded = true;
    loadConfig('keywords.yaml');
    const s = store('clipper.fc.streamer');
    if (s) { $('#fcStreamer').value = s; fcLoadSaved(); }
    $('#fcVod').value = store('clipper.fc.vod') || '';
  }
  requestAnimationFrame(fcDraw);
}

// --------------------------------------------------------------------------- captions page
const CF = {
  model: { label: 'Speech model', type: 'select',
    choices: [['tiny', 'Tiny (fastest, rough)'], ['base', 'Base (fast)'], ['small', 'Small (recommended)'],
      ['medium', 'Medium (more accurate, slower)'], ['large-v3', 'Large v3 (most accurate, slowest)'], ['turbo', 'Turbo (accurate, for fast PCs)']],
    help: 'Downloaded once, the first time it\'s used: tiny 75 MB, base 145 MB, small 480 MB, medium 1.5 GB, turbo 1.6 GB, large 3 GB.' },
  language: { label: 'Language', type: 'select',
    choices: [['auto', 'Detect for each clip'], ['en', 'English'], ['es', 'Spanish'], ['pt', 'Portuguese'], ['fr', 'French'], ['de', 'German'],
      ['it', 'Italian'], ['nl', 'Dutch'], ['pl', 'Polish'], ['tr', 'Turkish'], ['ru', 'Russian'], ['ar', 'Arabic'], ['ja', 'Japanese'], ['ko', 'Korean'], ['zh', 'Chinese']],
    help: 'Pick it when you know it. Detection can be fooled by loud game audio, or by a clip where someone speaks another language.' },
  device: { label: 'Run on', type: 'select', choices: [['auto', 'GPU if it works, else CPU'], ['cpu', 'CPU'], ['cuda', 'NVIDIA GPU']] },
  words_per_caption: { label: 'Words at a time', type: 'num', step: 1, min: 1 },
  font: { label: 'Font', type: 'text', placeholder: 'Arial Black', help: 'Any font installed on this PC, e.g. Arial Black, Impact, Segoe UI Black.' },
  size: { label: 'Text size', type: 'num', step: 2, min: 30 },
  color: { label: 'Text color', type: 'color' },
  highlight: { label: 'Spoken word color', type: 'color' },
  outline: { label: 'Outline', type: 'num', step: 1, min: 0 },
  position: { label: 'Position', type: 'select',
    choices: [['auto', 'Auto'], ['top', 'Top'], ['middle', 'Middle'], ['bottom', 'Bottom']],
    help: 'Auto: on the line between the facecam and the gameplay, or in the lower middle with center crop. Bottom stays above where TikTok and Shorts put their own buttons.' },
  uppercase: { label: 'ALL CAPS', type: 'bool' },
  mask_swears: { label: 'Mask swear words (f***)', type: 'bool' },
};
const CAP = { info: null, saved: null, savedForm: '', run: null, built: false, layout: 'stacked', frame: null, frameKey: null, t0: performance.now(), raf: 0 };
const CAP_SAMPLE = 'no way he actually hit that shot chat is going crazy right now';

async function loadCaptions() {
  if (!CAP.built) {
    CAP.built = true;
    $('#capOpts').innerHTML = Object.keys(CF).map((id) => fieldHTML('captions', id, CF)).join('');
    $$('#capOpts [data-k], #capVocab, #capReplace').forEach((el) => {
      el.addEventListener('input', capChanged);
      el.addEventListener('change', capChanged);
    });
    $$('#capPrevLayout button').forEach((b) => { b.onclick = () => capSetLayout(b.dataset.l); });
    $('#capSave').onclick = capSave;
    $('#capRevert').onclick = () => { capFill(CAP.saved); capChanged(); };
    $('#capRun').onchange = () => { CAP.run = $('#capRun').value; CAP.frameKey = null; renderCapRuns(); };
    $('#capGo').onclick = capGo;
  }
  try {
    CAP.info = await api('/api/captions');
    if (!CAP.saved || JSON.stringify(capRead()) === CAP.savedForm) { CAP.saved = CAP.info.config; capFill(CAP.saved); }
    renderCapStatus();
  } catch (e) { toast(e.message, true); }
  renderCapRuns();
  capAnimate();
}

function capRead() {
  const replace = {};
  for (const line of $('#capReplace').value.split('\n')) {
    const m = line.split(/\s*(?:=|->|→)\s*/);
    if (m.length >= 2 && m[0].trim() && m[1].trim()) replace[m[0].trim()] = m.slice(1).join(' ').trim();
  }
  return { ...readForm('captions'), vocabulary: $('#capVocab').value.split('\n').map((s) => s.trim()).filter(Boolean), replace };
}

function capFill(cfg) {
  if (!cfg) return;
  fillForm('captions', cfg);
  $('#capVocab').value = (cfg.vocabulary || []).join('\n');
  $('#capReplace').value = Object.entries(cfg.replace || {}).map(([k, v]) => `${k} = ${v}`).join('\n');
  CAP.savedForm = JSON.stringify(capRead());
}

function capChanged() {
  const dirty = JSON.stringify(capRead()) !== CAP.savedForm;
  $('#capPath').textContent = dirty ? 'Unsaved changes' : (CAP.info?.path || '');
}

async function capSave() {
  try {
    CAP.info = await api('/api/captions', { method: 'POST', body: { config: { ...CAP.saved, ...capRead() } } });
    CAP.saved = CAP.info.config;
    capFill(CAP.saved);
    capChanged();
    renderCapStatus();
    toast('Caption settings saved. They apply to the next clips you cut or caption.');
  } catch (e) { toast(e.message, true); }
}

function renderCapStatus() {
  const i = CAP.info;
  if (!i) return;
  const box = $('#capStatus');
  if (!i.available) {
    box.innerHTML = `<div class="cap-missing"><b>faster-whisper isn't installed yet.</b> In a terminal, in the project folder, run:
      <pre>${esc(i.install)}</pre>Then restart the dashboard.</div>`;
  } else {
    const where = i.gpu === 'ready' ? 'NVIDIA GPU' : i.gpu === 'no-libs' ? 'CPU (your NVIDIA GPU needs CUDA libraries first)' : 'CPU';
    box.innerHTML = `<span class="pill"><span class="dot"></span>faster-whisper ready</span>
      <span class="pill">Model: ${esc(i.config.model)}</span><span class="pill">Runs on: ${esc(where)}</span>`;
  }
  capChanged();
}

function capSetLayout(l) {
  CAP.layout = l;
  $$('#capPrevLayout button').forEach((x) => x.classList.toggle('active', x.dataset.l === l));
}

function renderCapRuns() {
  if (S.tab !== 'captions' || !CAP.built) return;
  const sel = $('#capRun');
  const runs = S.sessions.filter((s) => s.clips > 0);
  const opts = runs.map((s) => {
    const what = s.mode === 'live' ? `live ${s.name.split('_').slice(-2).join(' ')}` : s.name;
    return `<option value="${esc(s.name)}">${esc(s.streamer || s.name)} · ${esc(what)} · ${s.clips} clip${s.clips > 1 ? 's' : ''}</option>`;
  }).join('');
  if (sel.dataset.html !== opts) { sel.innerHTML = opts || '<option value="">No clips cut yet</option>'; sel.dataset.html = opts; }
  if (CAP.run && runs.some((s) => s.name === CAP.run)) sel.value = CAP.run;
  else CAP.run = sel.value || null;

  const name = CAP.run;
  const data = name && S.sess[name];
  if (name && !data) { loadSession(name); return; }
  const clips = data ? data.report.clips.filter((c) => c.file && data.files[c.file]) : [];
  const done = clips.filter((c) => c.captions).length;
  const busy = name && (captionJobFor(name) || Object.values(S.jobs).some((j) => j.running && j.sessions.includes(name)));
  const btn = $('#capGo');
  btn.disabled = !CAP.info?.available || !clips.length || !!busy;
  btn.textContent = !clips.length ? 'Add captions' : done === clips.length
    ? `Redo captions on ${clips.length} clip${clips.length > 1 ? 's' : ''}`
    : `Add captions to ${clips.length} clip${clips.length > 1 ? 's' : ''}`;
  $('#capRunInfo').textContent = !name ? 'Cut some clips first (VODs or Live page).'
    : busy ? 'This run is busy right now.'
      : `${clips.length} clip${clips.length === 1 ? '' : 's'}, ${done} with captions.`;

  // preview background: a frame from the first clip, without captions if there is a clean copy
  const c = clips[0];
  const key = c ? `${name}/${c.file}/${data.files[c.file]}` : null;
  if (key !== CAP.frameKey) {
    CAP.frameKey = key;
    CAP.frame = null;
    if (c) {
      if (c.layout) capSetLayout(c.layout === 'stacked' ? 'stacked' : 'center');
      const v = document.createElement('video');
      v.muted = true; v.preload = 'auto'; v.crossOrigin = 'anonymous';
      const path = c.captions ? `clean/${encodeURIComponent(c.file)}` : encodeURIComponent(c.file);
      v.src = `/media/${encodeURIComponent(name)}/${path}?v=${data.files[c.file]}`;
      v.addEventListener('loadedmetadata', () => { v.currentTime = Math.min(v.duration * 0.35, 12); });
      v.addEventListener('seeked', () => {
        if (CAP.frameKey !== key) return;
        const off = document.createElement('canvas');
        off.width = 270; off.height = 480;
        off.getContext('2d').drawImage(v, 0, 0, 270, 480);
        CAP.frame = off;
        v.removeAttribute('src'); v.load();
      }, { once: true });
    }
  }
}

function capY(position, layout) {
  if (position === 'top') return 330;
  if (position === 'middle') return 960;
  if (position === 'bottom') return 1480;
  return layout === 'stacked' ? 640 : 1300;
}

function capDraw() {
  const cv = $('#capPreview');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height, k = W / 1080;
  ctx.clearRect(0, 0, W, H);
  if (CAP.frame) {
    ctx.drawImage(CAP.frame, 0, 0, W, H);
  } else {
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, '#2b2f6b'); g.addColorStop(1, '#5b2a6e');
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    if (CAP.layout === 'stacked') {
      ctx.fillStyle = 'rgba(0,0,0,.25)'; ctx.fillRect(0, 0, W, 640 * k);
      ctx.fillStyle = 'rgba(255,255,255,.45)'; ctx.font = `${12}px system-ui`; ctx.textAlign = 'center';
      ctx.fillText('facecam', W / 2, 320 * k); ctx.fillText('gameplay', W / 2, 1300 * k);
    }
  }
  const cfg = { ...(CAP.saved || {}), ...readForm('captions') };
  const per = Math.max(1, Math.min(8, cfg.words_per_caption || 3));
  const words = CAP_SAMPLE.split(' ').map((w) => (cfg.uppercase ? w.toUpperCase() : w));
  const t = (performance.now() - CAP.t0) / 1000;
  const idx = Math.floor(t / 0.34) % words.length;
  const ci = Math.floor(idx / per);
  const chunk = words.slice(ci * per, ci * per + per);
  const hi = idx - ci * per;
  const size = (cfg.size || 84) * k;
  ctx.font = `900 ${size}px "${cfg.font || 'Arial Black'}", "Arial Black", Impact, sans-serif`;
  ctx.textBaseline = 'middle'; ctx.textAlign = 'left'; ctx.lineJoin = 'round';
  const space = ctx.measureText(' ').width;
  const widths = chunk.map((w) => ctx.measureText(w).width);
  let x = W / 2 - (widths.reduce((a, b) => a + b, 0) + space * (chunk.length - 1)) / 2;
  const y = capY(cfg.position || 'auto', CAP.layout) * k;
  chunk.forEach((w, i) => {
    ctx.save();
    const cx = x + widths[i] / 2;
    ctx.translate(cx, y);
    if (i === hi) ctx.scale(1.08, 1.08);
    if (cfg.outline > 0) {
      ctx.lineWidth = cfg.outline * 2 * k;
      ctx.strokeStyle = '#000';
      ctx.shadowColor = 'rgba(0,0,0,.55)'; ctx.shadowOffsetX = ctx.shadowOffsetY = 2 * k;
      ctx.strokeText(w, -widths[i] / 2, 0);
      ctx.shadowColor = 'transparent';
    }
    ctx.fillStyle = i === hi ? (cfg.highlight || '#4ade80') : (cfg.color || '#fff');
    ctx.fillText(w, -widths[i] / 2, 0);
    ctx.restore();
    x += widths[i] + space;
  });
}

function capAnimate() {
  cancelAnimationFrame(CAP.raf);
  const loop = () => {
    if (S.tab !== 'captions') return;
    capDraw();
    CAP.raf = requestAnimationFrame(loop);
  };
  loop();
}

async function capGo() {
  const name = CAP.run;
  if (!name) return;
  if (JSON.stringify(capRead()) !== CAP.savedForm && !confirm('You have unsaved caption settings. Captions use the saved ones. Continue anyway?')) return;
  const j = await startJob('captions', { session: name, retranscribe: $('#capRetrans').checked });
  if (j) S.sel.captions.session = name;
}

// --------------------------------------------------------------------------- start
async function init() {
  try {
    S.info = await api('/api/info');
  } catch (e) {
    document.body.innerHTML = `<div style="padding:40px">Couldn't reach the clipper. Run <code>clipper ui</code> again.<br>${esc(e.message)}</div>`;
    return;
  }
  buildForm('vod');
  buildForm('live');
  if (!S.info.captions) {
    $$('[data-k=captions]').forEach((el) => {
      el.checked = false; el.disabled = true;
      el.closest('label').title = 'Install faster-whisper first (see the Captions page).';
    });
  }
  renderTools();
  setupFacecam();
  setupConfig();

  $('#vodUrl').value = store('clipper.vodUrl') || '';
  $('#liveChannel').value = store('clipper.liveChannel') || '';
  $('#vodUrl').addEventListener('change', () => store('clipper.vodUrl', $('#vodUrl').value));
  $('#liveChannel').addEventListener('change', () => store('clipper.liveChannel', $('#liveChannel').value));

  $$('[data-tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
  $$('[data-reset]').forEach((b) => b.addEventListener('click', () => {
    const m = b.dataset.reset;
    fillForm(m, S.info.defaults[m]);
    store(`clipper.form.${m}`, readForm(m));
  }));
  $('#runList').addEventListener('click', (e) => {
    const b = e.target.closest('.run');
    if (!b) return;
    if (b.dataset.job) selectJob(b.dataset.job);
    else selectSession(b.dataset.session, b.dataset.mode);
  });
  $('#openOut').addEventListener('click', () => api('/api/open', { method: 'POST', body: {} }).catch((e) => toast(e.message, true)));

  const vodStart = (dry) => {
    const inputs = $('#vodUrl').value.trim();
    if (!inputs) { toast('Paste a Twitch VOD link first.', true); $('#vodUrl').focus(); return; }
    store('clipper.vodUrl', inputs);
    startJob('vod', { inputs: inputs.split(/\s+/), options: { ...readForm('vod'), dry_run: dry } });
  };
  $('#vodPreview').addEventListener('click', () => vodStart(true));
  $('#vodCut').addEventListener('click', () => vodStart(false));
  $('#vodUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') vodStart(true); });
  const liveStart = () => {
    const ch = $('#liveChannel').value.trim();
    if (!ch) { toast('Type a channel name first.', true); $('#liveChannel').focus(); return; }
    const running = Object.values(S.jobs).find((j) => j.running && j.mode === 'live' && j.argv[1] === ch.toLowerCase());
    if (running && !confirm(`Already watching ${ch}. Start a second one anyway?`)) return;
    store('clipper.liveChannel', ch);
    startJob('live', { channel: ch, options: readForm('live') });
  };
  $('#liveStart').addEventListener('click', liveStart);
  $('#liveChannel').addEventListener('keydown', (e) => { if (e.key === 'Enter') liveStart(); });

  await refreshSessions();
  setInterval(refreshSessions, 6000);
  const tab = store('clipper.tab');
  setTab(['vod', 'live', 'captions', 'setup'].includes(tab) ? tab : 'vod');
  // Show what's running (e.g. after reloading the page mid-stream), else the latest run.
  try {
    for (const j of await api('/api/jobs')) S.jobs[j.id] = j;
  } catch { /* offline */ }
  for (const mode of ['vod', 'live']) {
    const j = Object.values(S.jobs).filter((x) => x.mode === mode).sort((a, b) => (b.running - a.running) || (b.started - a.started))[0];
    if (j && (j.running || !S.sessions.length)) {
      S.sel[mode] = { job: j.id, session: j.sessions[0] && mode === 'live' ? j.sessions[0] : j.sessions[j.sessions.length - 1] || null };
    } else {
      const s = S.sessions.find((x) => x.mode === mode);
      if (s) S.sel[mode] = { job: jobForSession(s.name)?.id || null, session: s.name };
    }
    if (S.sel[mode].session) loadSession(S.sel[mode].session);
  }
  tick();
}

init();
