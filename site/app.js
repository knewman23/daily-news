/* Daily News — vanilla, no build step, no CDN.
 *
 * Two modes, decided by `<body data-mode>`:
 *
 *   live    — talks to the local Python server. Everything works.
 *   static  — the exported GitHub Pages build. Reads pre-generated JSON, and the
 *             journal, source list, and run history are absent, because there is
 *             no server to write to and the source list is not for publishing.
 *
 * The mode comes from the markup rather than the hostname: hostname sniffing
 * breaks the moment the local server is reached by anything but 127.0.0.1.
 *
 * The server renders each day's markdown to HTML, so the main pane sets
 * innerHTML from it. That content is generated locally by this project from the
 * user's own files. Everything that comes from outside — handles, headlines, log
 * lines — goes in as textContent.
 */

'use strict';

const READ_ONLY = document.body.dataset.mode === 'static';

const state = {
  days: [],
  activeDate: null,
  activeTag: '',
  query: '',
  notesDirty: false,
  searchIndex: null,     // static mode only
};

const $ = (id) => document.getElementById(id);

const el = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of [].concat(children)) {
    if (child != null) node.append(child);
  }
  return node;
};

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* an empty or non-JSON body is fine; the status carries the meaning */
  }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `${response.status} ${response.statusText}`);
  }
  return payload;
}

const postJSON = (path, body, method = 'POST') => api(path, {
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

/* One place decides where data comes from, so adding the static build did not
   mean sprinkling conditionals through every fetch. */
const source = {
  days: () => READ_ONLY ? 'data/days.json' : '/api/days',
  tags: () => READ_ONLY ? 'data/tags.json' : '/api/tags',
  runs: () => '/api/runs',
  day: (date) => READ_ONLY ? `data/day/${date}.json` : `/api/day/${date}`,
  log: (date) => `/api/log/${date}`,
  searchIndex: () => 'data/search.json',
};

/* --- days ---------------------------------------------------------------- */

async function loadDays() {
  const { days } = await api(source.days());
  state.days = days;
  renderDays();
  return days;
}

function renderDays() {
  const list = $('days');

  if (!state.days.length) {
    list.replaceChildren(el('li', {}, el('p', {
      className: 'empty', textContent: 'No digests yet.',
    })));
    return;
  }

  list.replaceChildren(...state.days.map((day) => {
    const count = el('span', {
      className: 'topics',
      textContent: day.post_count ? `${day.post_count}` : '—',
    });
    const button = el('button', { type: 'button' }, [
      el('span', { className: 'label', textContent: prettyDate(day.date) }),
      count,
    ]);

    if (day.incomplete) {
      count.append(el('span', { className: 'flag', textContent: ' !' }));
      button.title = READ_ONLY
        ? 'This day may be missing posts'
        : 'This run was incomplete — see the Runs panel';
    }
    if (day.date === state.activeDate) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', () => {
      closeDrawer();
      showDay(day.date);
    });

    return el('li', {}, button);
  }));
}

async function showDay(date, scrollToHeadline) {
  if (!(await confirmDiscardNotes())) return;

  state.activeDate = date;
  state.query = '';
  state.activeTag = '';
  $('search').value = '';
  renderDays();
  renderTags();
  syncActiveFilter();

  const day = await api(source.day(date));
  const main = $('main');

  const head = el('div', { className: 'day-head' }, [
    el('h2', { textContent: prettyDate(date, true) }),
    el('span', {
      className: 'day-meta',
      textContent: `${day.post_count} post${day.post_count === 1 ? '' : 's'}`
        + ` · ${day.transcribed_count} with text`,
    }),
  ]);

  if (day.incomplete) {
    head.append(el('p', {
      className: 'warn-banner',
      textContent: READ_ONLY
        ? 'This run did not finish cleanly, so the day may be missing posts.'
        : 'This run did not finish cleanly, so the day may be missing posts. '
          + 'Open the Runs panel for the reason.',
    }));
  }

  const article = el('article', { className: 'article' });
  article.innerHTML = day.html;      // locally generated markdown; see file header

  const parts = [head, article];
  if (!READ_ONLY) parts.push(journal(date, day.notes));
  main.replaceChildren(...parts);

  if (scrollToHeadline) {
    const match = [...article.querySelectorAll('h2')]
      .find((h) => h.textContent.trim() === scrollToHeadline.trim());
    if (match) {
      match.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
  }
  window.scrollTo({ top: 0 });
}

/* --- journal (live only) ------------------------------------------------- */

function journal(date, existing) {
  const box = el('textarea', {
    id: 'notes',
    placeholder: 'How do you feel about today?',
    value: existing || '',
  });
  const status = el('span', { className: 'status', id: 'notes-status' });
  const save = el('button', { className: 'notes-save', type: 'button', textContent: 'Save notes' });

  let saved = existing || '';

  const setStatus = (text, kind = '') => {
    status.textContent = text;
    status.className = `status ${kind}`;
  };

  box.addEventListener('input', () => {
    state.notesDirty = box.value !== saved;
    setStatus(state.notesDirty ? 'Unsaved' : '');
  });

  save.addEventListener('click', async () => {
    save.disabled = true;
    setStatus('Saving…');
    try {
      const result = await postJSON(`/api/notes/${date}`, { notes: box.value });
      saved = result.notes;
      box.value = saved;
      state.notesDirty = false;
      setStatus('Saved', 'saved');
    } catch (error) {
      // A 409 means the file's marker block is broken. Say so plainly rather
      // than letting the user retype into a box that will never persist.
      setStatus(error.message, 'failed');
    } finally {
      save.disabled = false;
    }
  });

  // Cmd/Ctrl-S is the reflex for a text box; honour it.
  box.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 's') {
      event.preventDefault();
      save.click();
    }
  });

  return el('section', { className: 'journal' }, [
    el('h3', { textContent: 'My notes' }),
    box,
    el('div', { className: 'journal-actions' }, [save, status]),
  ]);
}

async function confirmDiscardNotes() {
  if (!state.notesDirty) return true;
  const ok = window.confirm('You have unsaved notes. Leave this day and lose them?');
  if (ok) state.notesDirty = false;
  return ok;
}

/* --- search and tags ----------------------------------------------------- */

let allTags = [];

async function loadTags() {
  ({ tags: allTags } = await api(source.tags()));
  renderTags();
}

function renderTags() {
  $('tags').replaceChildren(...allTags.map((tag) => {
    const chip = el('button', { type: 'button', className: 'chip', textContent: tag });
    chip.setAttribute('aria-pressed', String(state.activeTag === tag));
    chip.addEventListener('click', () => {
      state.activeTag = state.activeTag === tag ? '' : tag;
      renderTags();
      closeDrawer();
      runSearch();
    });
    return chip;
  }));

  if (!allTags.length) {
    $('tags').replaceChildren(el('p', { className: 'empty', textContent: 'No topics yet.' }));
  }
}

async function runSearch() {
  syncActiveFilter();

  if (!state.query.trim() && !state.activeTag) {
    if (state.activeDate) await showDay(state.activeDate);
    return;
  }
  if (!(await confirmDiscardNotes())) return;

  const hits = READ_ONLY
    ? searchLocally(state.query, state.activeTag)
    : (await api(`/api/search?${new URLSearchParams({
        q: state.query, tag: state.activeTag,
      })}`)).hits;

  const heading = el('div', { className: 'day-head' }, [
    el('h2', {
      textContent: hits.length
        ? `${hits.length} match${hits.length === 1 ? '' : 'es'}`
        : 'No matches',
    }),
    el('span', { className: 'day-meta', textContent: describeFilters() }),
  ]);

  if (!hits.length) {
    $('main').replaceChildren(heading, el('p', {
      className: 'empty', textContent: 'Nothing in the archive matches that.',
    }));
    return;
  }

  const list = el('ul', { className: 'hits' }, hits.map((hit) => {
    const open = el('button', { type: 'button', className: 'hit-head', textContent: hit.headline });
    open.addEventListener('click', () => showDay(hit.date, hit.headline));

    return el('li', {}, [
      open,
      el('div', { className: 'hit-date', textContent: prettyDate(hit.date, true) }),
      el('p', { className: 'hit-snippet', textContent: hit.snippet }),
    ]);
  }));

  $('main').replaceChildren(heading, list);
  window.scrollTo({ top: 0 });
}

/* Static mode has no server to search, so the exported index is filtered here.
   Same rules as digest.search: an empty query with no tag matches nothing, days
   come newest first, and a topic must match both the query and the tag. */
function searchLocally(query, tag) {
  const needle = (query || '').trim().toLowerCase();
  const wanted = (tag || '').trim().toLowerCase();
  if (!needle && !wanted) return [];

  return (state.searchIndex || []).filter((entry) => {
    if (wanted && !entry.tags.includes(wanted)) return false;
    if (needle && !entry.text.includes(needle)) return false;
    return true;
  });
}

function describeFilters() {
  const parts = [];
  if (state.query.trim()) parts.push(`“${state.query.trim()}”`);
  if (state.activeTag) parts.push(`tagged ${state.activeTag}`);
  return parts.join(' · ');
}

function syncActiveFilter() {
  const button = $('active-filter');
  button.hidden = !state.activeTag;
  button.textContent = state.activeTag;
  button.title = `Clear the ${state.activeTag} filter`;
}

/* --- sources (live only) ------------------------------------------------- */

const QUIET_DAYS_BEFORE_SUSPECT = 4;

function isProbablyDead(entry) {
  if (!entry.enabled || entry.last_seen || !entry.added) return false;
  const added = new Date(`${entry.added}T00:00:00`);
  return (Date.now() - added.getTime()) / 86_400_000 >= QUIET_DAYS_BEFORE_SUSPECT;
}

async function loadSources() {
  const { sources } = await api('/api/sources');
  const active = sources.filter((s) => s.enabled).length;
  $('sources-count').textContent = `(${active}/${sources.length})`;

  $('sources').replaceChildren(...sources.map((entry) => {
    const handle = el('span', { className: 'handle', textContent: `@${entry.handle}` });
    if (isProbablyDead(entry)) {
      // A handle watched for a while that has never yielded a post is usually a
      // typo or a renamed account. A few quiet days is not evidence — plenty of
      // real accounts post rarely — so this waits before complaining.
      handle.append(el('span', {
        className: 'stale',
        textContent: ' ?',
        title: `Watched since ${entry.added} with no post found. `
          + 'Check the handle is still correct.',
      }));
    }

    const toggle = el('button', {
      type: 'button',
      className: 'icon-button',
      textContent: entry.enabled ? '◉' : '○',
      title: entry.enabled ? 'Disable (stop pulling new posts)' : 'Enable',
    });
    toggle.addEventListener('click', async () => {
      await postJSON(`/api/sources/${encodeURIComponent(entry.handle)}`,
                     { enabled: !entry.enabled }, 'PATCH');
      loadSources();
    });

    const remove = el('button', {
      type: 'button', className: 'icon-button', textContent: '✕',
      title: 'Delete from the list',
    });
    remove.addEventListener('click', async () => {
      // Deleting is the only destructive action in the UI, so it asks.
      if (!window.confirm(`Delete @${entry.handle}? Disabling keeps its past `
        + 'contributions attributable.')) return;
      await api(`/api/sources/${encodeURIComponent(entry.handle)}`, { method: 'DELETE' });
      loadSources();
    });

    return el('li', { className: entry.enabled ? '' : 'off' }, [handle, toggle, remove]);
  }));
}

function wireAddSource() {
  const form = $('add-source');
  const input = $('new-handle');
  const error = $('add-error');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const handle = input.value.trim();
    if (!handle) return;

    error.hidden = true;
    const button = form.querySelector('button');
    button.disabled = true;
    button.textContent = 'Checking…';

    try {
      await postJSON('/api/sources', { handle });
      input.value = '';
      await loadSources();
    } catch (failure) {
      // The server checks the account is reachable before adding. Show why it
      // refused, inline — otherwise a typo looks like a broken button.
      error.textContent = failure.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = 'Add';
    }
  });
}

/* --- runs (live only) ---------------------------------------------------- */

async function loadRuns() {
  const { runs, problems } = await api(source.runs());

  const badge = $('runs-badge');
  badge.hidden = !problems;
  badge.textContent = String(problems);
  if (problems) badge.title = `${problems} run(s) with problems`;

  if (!runs.length) {
    $('runs').replaceChildren(el('li', {}, el('p', {
      className: 'empty', textContent: 'No runs recorded yet.',
    })));
    return;
  }

  $('runs').replaceChildren(...runs.map(renderRun));
}

function renderRun(run) {
  const bad = !run.ok;
  const dot = el('span', {
    className: `run-dot ${bad ? 'bad' : run.incomplete ? 'warn' : 'ok'}`,
    textContent: bad ? '✕' : run.incomplete ? '▲' : '●',
  });

  const head = el('button', { type: 'button', className: 'run-head' }, [
    dot,
    el('span', { textContent: run.date }),
    el('span', {
      className: 'run-meta',
      textContent: `${run.topic_count || 0} topics · ${formatDuration(run.duration_seconds)}`,
    }),
  ]);

  const detail = el('div', { className: 'run-detail', hidden: true });

  if (run.error) detail.append(el('p', { className: 'error', textContent: run.error }));
  if (run.failures && run.failures.length) {
    detail.append(el('ul', {}, run.failures.map((note) => el('li', { textContent: note }))));
  }
  if (!run.error && !(run.failures || []).length) {
    detail.append(el('p', {
      className: 'status',
      textContent: `${run.spoken_count || 0} spoken · ${run.image_count || 0} on-image`
        + ` · ${run.post_count || 0} posts`,
    }));
  }

  const showLog = el('button', {
    type: 'button', className: 'run-log-button', textContent: 'View log',
  });
  showLog.addEventListener('click', async () => {
    showLog.disabled = true;
    try {
      const { log } = await api(source.log(run.date));
      detail.append(renderLog(log));
      showLog.remove();
    } catch (failure) {
      detail.append(el('p', { className: 'error', textContent: failure.message }));
      showLog.disabled = false;
    }
  });
  detail.append(showLog);

  head.addEventListener('click', () => { detail.hidden = !detail.hidden; });
  return el('li', {}, [head, detail]);
}

function renderLog(text) {
  // Highlight the lines worth acting on. Log content is inserted as text.
  const view = el('pre', { className: 'log-view' });
  for (const line of text.split('\n')) {
    const kind = /ERROR|Traceback|CRITICAL/.test(line) ? 'line-error'
      : /WARNING/.test(line) ? 'line-warn' : '';
    view.append(el('span', { className: kind, textContent: line + '\n' }));
  }
  return view;
}

function formatDuration(seconds) {
  const total = Math.round(seconds || 0);
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
}

/* --- drawer -------------------------------------------------------------- */

/* The panels are a persistent rail on desktop and a slide-out drawer on phones.
   Which one applies is decided here rather than by CSS alone, because the drawer
   is toggled with the `hidden` attribute: leaving it set on a desktop-width
   window would hide the rail from assistive technology even if CSS drew it. The
   breakpoint matches the one in style.css. */
const PHONE = window.matchMedia('(max-width: 46rem)');

function openDrawer() {
  if (!PHONE.matches) return;
  $('drawer').hidden = false;
  $('scrim').hidden = false;
  document.body.classList.add('drawer-open');
  $('menu-button').setAttribute('aria-expanded', 'true');
  $('drawer-close').focus();
}

function closeDrawer() {
  if (!PHONE.matches || $('drawer').hidden) return;
  $('drawer').hidden = true;
  $('scrim').hidden = true;
  document.body.classList.remove('drawer-open');
  $('menu-button').setAttribute('aria-expanded', 'false');
  $('menu-button').focus();
}

function applyLayout() {
  const drawer = $('drawer');
  if (PHONE.matches) {
    drawer.hidden = true;
    $('scrim').hidden = true;
    document.body.classList.remove('drawer-open');
    $('menu-button').setAttribute('aria-expanded', 'false');
  } else {
    // Desktop: always present, never a dialog.
    drawer.hidden = false;
    $('scrim').hidden = true;
    document.body.classList.remove('drawer-open');
    $('menu-button').removeAttribute('aria-expanded');
  }
}

function wireDrawer() {
  applyLayout();
  // Rotating a phone or dragging a window across the breakpoint must not leave
  // the rail stuck hidden, or the drawer stuck open over a wide layout.
  PHONE.addEventListener('change', applyLayout);

  $('menu-button').addEventListener('click', () => {
    if ($('drawer').hidden) openDrawer(); else closeDrawer();
  });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('scrim').addEventListener('click', closeDrawer);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDrawer();
  });
}

/* --- dates --------------------------------------------------------------- */

function prettyDate(iso, long = false) {
  const [year, month, day] = iso.split('-').map(Number);
  // Construct locally, not from the ISO string: `new Date('2026-07-28')` parses
  // as UTC midnight and renders as the 27th in any negative-offset timezone.
  const date = new Date(year, month - 1, day);
  return date.toLocaleDateString(undefined, long
    ? { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }
    : { month: 'short', day: 'numeric' });
}

/* --- start --------------------------------------------------------------- */

function wireToolbar() {
  let timer;
  $('search').addEventListener('input', (event) => {
    state.query = event.target.value;
    clearTimeout(timer);
    timer = setTimeout(runSearch, 180);
  });

  $('active-filter').addEventListener('click', () => {
    state.activeTag = '';
    renderTags();
    runSearch();
  });

  window.addEventListener('beforeunload', (event) => {
    if (state.notesDirty) event.preventDefault();
  });
}

async function start() {
  // Remove the live-only panels outright in the static build. Hiding them with
  // CSS would leave a working add-a-handle form for anyone opening devtools.
  if (READ_ONLY) {
    for (const node of document.querySelectorAll('[data-live-only]')) node.remove();
  }

  wireToolbar();
  wireDrawer();
  if (!READ_ONLY) wireAddSource();

  try {
    const work = [loadDays(), loadTags()];
    if (READ_ONLY) {
      work.push(api(source.searchIndex())
        .then(({ topics }) => { state.searchIndex = topics; })
        .catch(() => { state.searchIndex = []; }));
    } else {
      work.push(loadSources(), loadRuns());
    }

    const [days] = await Promise.all(work);

    if (days.length) {
      await showDay(days[0].date);
    } else {
      $('main').replaceChildren(el('p', {
        className: 'empty',
        textContent: READ_ONLY
          ? 'No digests published yet.'
          : 'No digests yet. Run: .venv/bin/python run_daily.py',
      }));
    }

    // Surface a run that needs attention. On desktop the rail is already
    // visible, so expanding the panel is enough.
    if (!READ_ONLY && !$('runs-badge').hidden) {
      $('runs-panel').open = true;
      openDrawer();
    }
  } catch (failure) {
    $('main').replaceChildren(el('p', {
      className: 'error', textContent: `Could not load: ${failure.message}`,
    }));
  }
}

start();
