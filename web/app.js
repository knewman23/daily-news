/* Daily News — vanilla, no build step, no CDN.
 *
 * The server renders each day's markdown to HTML, so the main pane sets
 * innerHTML from it. That content is generated locally by this project from the
 * user's own files and never leaves the machine, so there is no untrusted author
 * in the loop. Everything that *does* come from outside — handles, headlines,
 * log lines — goes in as textContent.
 */

'use strict';

const state = {
  days: [],
  tags: [],
  activeDate: null,
  activeTag: '',
  query: '',
  notesDirty: false,
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

/* --- days ---------------------------------------------------------------- */

async function loadDays() {
  const { days } = await api('/api/days');
  state.days = days;
  renderDays();
  return days;
}

function renderDays() {
  const list = $('days');
  list.replaceChildren(...state.days.map((day) => {
    const label = el('span', { className: 'label', textContent: prettyDate(day.date) });
    const count = el('span', {
      className: 'topics',
      textContent: day.post_count ? `${day.post_count}` : '—',
    });

    const button = el('button', { type: 'button' }, [label, count]);
    if (day.incomplete) {
      count.append(el('span', { className: 'flag', textContent: ' !' }));
      button.title = 'This run was incomplete — see the Runs panel';
    }
    if (day.date === state.activeDate) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', () => showDay(day.date));

    return el('li', {}, button);
  }));

  if (!state.days.length) {
    list.replaceChildren(el('li', {}, el('p', {
      className: 'empty', textContent: 'No digests yet.',
    })));
  }
}

async function showDay(date, scrollToHeadline) {
  if (!(await confirmDiscardNotes())) return;

  state.activeDate = date;
  state.query = '';
  $('search').value = '';
  renderDays();
  syncClearButton();

  const day = await api(`/api/day/${date}`);
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
      textContent: 'This run did not finish cleanly, so the day may be missing '
        + 'posts. Open the Runs panel for the reason.',
    }));
  }

  const article = el('article', { className: 'article' });
  article.innerHTML = day.html;      // locally generated markdown; see file header

  main.replaceChildren(head, article, journal(date, day.notes));

  if (scrollToHeadline) {
    const match = [...article.querySelectorAll('h2')]
      .find((h) => h.textContent.trim() === scrollToHeadline.trim());
    if (match) match.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } else {
    window.scrollTo({ top: 0 });
  }
}

/* --- journal ------------------------------------------------------------- */

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

async function loadTags() {
  const { tags } = await api('/api/tags');
  state.tags = tags;

  $('tags').replaceChildren(...tags.map((tag) => {
    const chip = el('button', {
      type: 'button', className: 'chip', textContent: tag,
    });
    chip.setAttribute('aria-pressed', String(state.activeTag === tag));
    chip.addEventListener('click', () => {
      state.activeTag = state.activeTag === tag ? '' : tag;
      loadTags();
      runSearch();
    });
    return chip;
  }));
}

async function runSearch() {
  syncClearButton();

  if (!state.query.trim() && !state.activeTag) {
    if (state.activeDate) await showDay(state.activeDate);
    return;
  }
  if (!(await confirmDiscardNotes())) return;

  const params = new URLSearchParams({ q: state.query, tag: state.activeTag });
  const { hits } = await api(`/api/search?${params}`);

  const heading = el('div', { className: 'day-head' }, [
    el('h2', { textContent: hits.length
      ? `${hits.length} match${hits.length === 1 ? '' : 'es'}`
      : 'No matches' }),
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

function describeFilters() {
  const parts = [];
  if (state.query.trim()) parts.push(`“${state.query.trim()}”`);
  if (state.activeTag) parts.push(`tagged ${state.activeTag}`);
  return parts.join(' · ');
}

function syncClearButton() {
  $('clear').hidden = !state.query.trim() && !state.activeTag;
}

/* --- sources ------------------------------------------------------------- */

async function loadSources() {
  const { sources } = await api('/api/sources');
  const active = sources.filter((s) => s.enabled).length;
  $('sources-count').textContent = `(${active}/${sources.length})`;

  $('sources').replaceChildren(...sources.map((source) => {
    const handle = el('span', { className: 'handle', textContent: `@${source.handle}` });
    if (isProbablyDead(source)) {
      // A handle that has been watched for a while and never yielded a post is
      // usually a typo or a renamed account. Days of silence alone is not
      // evidence — plenty of real accounts simply post rarely — so this waits
      // before complaining.
      handle.append(el('span', {
        className: 'stale',
        textContent: ' ?',
        title: `Watched since ${source.added} with no post found. `
          + 'Check the handle is still correct.',
      }));
    }

    const toggle = el('button', {
      type: 'button',
      className: 'icon-button',
      textContent: source.enabled ? '◉' : '○',
      title: source.enabled ? 'Disable (stop pulling new posts)' : 'Enable',
    });
    toggle.addEventListener('click', async () => {
      await postJSON(`/api/sources/${encodeURIComponent(source.handle)}`,
                     { enabled: !source.enabled }, 'PATCH');
      loadSources();
    });

    const remove = el('button', {
      type: 'button', className: 'icon-button', textContent: '✕',
      title: 'Delete from the list',
    });
    remove.addEventListener('click', async () => {
      // Deleting is the only destructive action in the UI, so it asks.
      if (!window.confirm(`Delete @${source.handle}? Disabling keeps its past `
        + 'contributions attributable.')) return;
      await api(`/api/sources/${encodeURIComponent(source.handle)}`, { method: 'DELETE' });
      loadSources();
    });

    return el('li', { className: source.enabled ? '' : 'off' }, [handle, toggle, remove]);
  }));
}

const QUIET_DAYS_BEFORE_SUSPECT = 4;

function isProbablyDead(source) {
  if (!source.enabled || source.last_seen || !source.added) return false;
  const added = new Date(`${source.added}T00:00:00`);
  const days = (Date.now() - added.getTime()) / 86_400_000;
  return days >= QUIET_DAYS_BEFORE_SUSPECT;
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

/* --- runs ---------------------------------------------------------------- */

async function loadRuns() {
  const { runs, problems } = await api('/api/runs');

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

  const row = el('li', {}, head);
  const detail = el('div', { className: 'run-detail', hidden: true });

  if (run.error) {
    detail.append(el('p', { className: 'error', textContent: run.error }));
  }
  if (run.failures && run.failures.length) {
    detail.append(el('ul', {}, run.failures.map(
      (note) => el('li', { textContent: note }),
    )));
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
      const { log } = await api(`/api/log/${run.date}`);
      detail.querySelector('.log-view')?.remove();
      detail.append(renderLog(log));
      showLog.remove();
    } catch (failure) {
      detail.append(el('p', { className: 'error', textContent: failure.message }));
      showLog.disabled = false;
    }
  });
  detail.append(showLog);

  head.addEventListener('click', () => { detail.hidden = !detail.hidden; });
  row.append(detail);
  return row;
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

  $('clear').addEventListener('click', () => {
    state.query = '';
    state.activeTag = '';
    $('search').value = '';
    loadTags();
    runSearch();
  });

  window.addEventListener('beforeunload', (event) => {
    if (state.notesDirty) event.preventDefault();
  });
}

async function start() {
  wireToolbar();
  wireAddSource();

  try {
    const [days] = await Promise.all([loadDays(), loadTags(), loadSources(), loadRuns()]);
    if (days.length) {
      await showDay(days[0].date);
    } else {
      $('main').replaceChildren(el('p', {
        className: 'empty',
        textContent: 'No digests yet. Run: .venv/bin/python run_daily.py',
      }));
    }
    // Open the Runs panel unprompted when something needs attention.
    if (!$('runs-badge').hidden) $('runs-panel').open = true;
  } catch (failure) {
    $('main').replaceChildren(el('p', {
      className: 'error', textContent: `Could not load: ${failure.message}`,
    }));
  }
}

start();
