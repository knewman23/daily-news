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

const SKIPPED = '__skipped__';   // a pseudo-tag: not a topic, a view

/* The clock the pipeline runs on. Pinned rather than using the viewer's local
   time so the stamp always describes the same clock that decides which day a
   post belongs to — otherwise reading the site from another timezone would show
   an update time that disagrees with the day headings. */
const DISPLAY_TZ = 'America/Denver';

const state = {
  days: [],
  activeDate: null,
  activeTag: '',
  query: '',
  notesDirty: false,
  searchIndex: null,     // static mode only
  /* Which month sections are expanded, as "YYYY-MM". Held here rather than read
     off the <details> elements because renderDays() rebuilds them on every
     showDay(), which would throw a DOM-only answer away. */
  openMonths: new Set(),
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
  const { days, last_updated: lastUpdated } = await api(source.days());
  state.days = days;
  renderDays();
  renderLastUpdated(lastUpdated);
  return days;
}

function renderLastUpdated(stamp) {
  const line = $('last-updated');
  if (!stamp) {
    line.textContent = '';
    return;
  }
  const when = new Date(stamp);
  if (Number.isNaN(when.getTime())) {
    line.textContent = '';
    return;
  }
  line.textContent = `Last updated ${when.toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
    timeZone: DISPLAY_TZ,
    timeZoneName: 'short',      // renders MDT in summer, MST in winter
  })}`;
}

function skippedForVisibleDays() {
  /* Every skip across the archive, newest day first, so the chip answers "what
     has the filter been dropping" rather than only "what did today drop". */
  return state.days.flatMap((day) => (day.skipped || []).map((entry) => ({
    date: day.date,
    headline: entry.headline,
    reason: entry.reason,
  })));
}

/* Days newest-first into month runs, preserving order. Sequential rather than a
   keyed map because the input is already sorted, so a run break is a new month. */
function groupByMonth(days) {
  const groups = [];
  for (const day of days) {
    const month = monthOf(day.date);
    const last = groups[groups.length - 1];
    if (last && last.month === month) last.days.push(day);
    else groups.push({ month, days: [day] });
  }
  return groups;
}

function renderDays() {
  const list = $('days');

  if (!state.days.length) {
    list.replaceChildren(el('p', { className: 'empty', textContent: 'No digests yet.' }));
    return;
  }

  // The day being read must be reachable, so its month is always expanded —
  // otherwise opening a search hit from March scrolls to a day the rail hides.
  // With nothing active yet, the newest month opens so the panel is not all shut.
  if (state.activeDate) state.openMonths.add(monthOf(state.activeDate));
  else if (!state.openMonths.size) state.openMonths.add(monthOf(state.days[0].date));

  list.replaceChildren(...groupByMonth(state.days).map((group) => {
    const section = el('details', {
      className: 'month',
      open: state.openMonths.has(group.month),
    });

    section.append(
      el('summary', { className: 'month-head' }, [
        el('span', { className: 'month-label', textContent: prettyMonth(group.month) }),
        el('span', {
          className: 'month-count',
          textContent: `${group.days.length}`,
          title: `${group.days.length} day${group.days.length === 1 ? '' : 's'}`,
        }),
      ]),
      el('ol', { className: 'month-days' }, group.days.map(dayRow)),
    );

    section.addEventListener('toggle', () => {
      if (section.open) state.openMonths.add(group.month);
      else state.openMonths.delete(group.month);
    });

    return section;
  }));
}

function dayRow(day) {
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
  indexHeadings(article);
  if (!READ_ONLY) addSkipControls(article, date);

  // The feed renders only topics the filter kept, so a day can legitimately have
  // posts and no feed. Saying so beats an unexplained blank page.
  if (!article.querySelector('h2')) {
    article.append(el('p', {
      className: 'empty',
      textContent: (day.skipped || []).length
        ? 'Everything from this day was left out as off topic.'
        : 'Nothing was found for this day.',
    }));
  }

  const parts = [head, article];
  const nav = dayNav(date);
  if (nav) parts.push(nav);
  // The journal stays last: the nav closes the reading, the notes box follows it.
  if (!READ_ONLY) parts.push(journal(date, day.notes));
  main.replaceChildren(...parts);

  if (scrollToHeadline) {
    const match = [...article.querySelectorAll('h2')]
      .find((h) => h.dataset.headline === scrollToHeadline.trim());
    if (match) {
      match.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
  }
  window.scrollTo({ top: 0 });
}

/* --- skipping (live only) ------------------------------------------------ */

/* Record each heading's text before anything is added around it, so a topic can
   still be identified once the markup has changed. */
function indexHeadings(article) {
  for (const heading of article.querySelectorAll('h2')) {
    heading.dataset.headline = heading.textContent.trim();
  }
}

/* The button is a *sibling* of the heading, inside a flex row, not a child of it.
   Nesting it in the h2 reads fine but drops the heading out of the accessibility
   tree entirely — and headings are how the page is navigated without sight.
   Verified in the a11y tree, not assumed: with the button inside, Chrome reported
   the h2 as bare text. CSS puts it back on the heading line. */
function addSkipControls(article, date) {
  for (const heading of [...article.querySelectorAll('h2')]) {
    const headline = heading.dataset.headline;
    const button = el('button', {
      type: 'button',
      className: 'skip-topic',
      title: `Leave “${headline}” out of the feed`,
    }, el('span', { className: 'visually-hidden', textContent: `Skip ${headline}` }));
    button.append(el('span', { ariaHidden: 'true', textContent: 'skip' }));
    button.addEventListener('click', () => skipTopic(date, headline, button));

    const row = el('div', { className: 'topic-head' });
    heading.before(row);
    row.append(heading, button);
  }
}

async function skipTopic(date, headline, button) {
  // Asked rather than assumed: the skipped list exists so that nothing is
  // dropped without a visible reason, and a hand-skip with no reason would be
  // the one silent drop in the system.
  const reason = window.prompt(`Why skip “${headline}”?`, 'marked by hand');
  if (reason === null) return;

  button.disabled = true;
  try {
    await postJSON(`/api/skip/${date}`, { headline, skipped: true, reason });
  } catch (failure) {
    button.disabled = false;
    window.alert(`Could not skip that topic: ${failure.message}`);
    return;
  }
  await afterSkipChange(date);
}

async function restoreTopic(date, headline, button) {
  button.disabled = true;
  try {
    await postJSON(`/api/skip/${date}`, { headline, skipped: false });
  } catch (failure) {
    button.disabled = false;
    window.alert(`Could not restore that topic: ${failure.message}`);
    return;
  }
  await afterSkipChange(date);
}

/* The day list carries the skip counts and the chip is built from it, so the
   index has to be reloaded before the view is redrawn — not just the day. */
async function afterSkipChange(date) {
  await loadDays();
  await loadTags();
  watchPublish();
  if (state.activeTag === SKIPPED) {
    renderTags();
    showSkipped();
    return;
  }
  await showDay(date);
}

/* --- republishing -------------------------------------------------------- */

/* The server coalesces edits and pushes once they stop, so the outcome arrives
   well after the request that caused it. Poll until it settles.

   Reported rather than left silent: a background push that failed would
   otherwise leave the reader believing the live site matches when it does not. */

const PUBLISH_TEXT = {
  pending: 'publishing soon…',
  publishing: 'publishing…',
  published: 'published ✓',
  failed: 'publish failed ⚠',
};

let publishTimer = null;

function renderPublish(status) {
  const line = $('publish-status');
  if (!line) return;                       // absent in the published build

  const text = PUBLISH_TEXT[status.state] || '';
  line.textContent = text;
  line.className = `publish-status ${status.state}`;
  line.title = status.message || '';
  // A failure is the one state worth interrupting for; the message names the
  // git error, which the status line has no room for.
  if (status.state === 'failed' && status.message) line.title = status.message;
}

function watchPublish() {
  clearTimeout(publishTimer);

  const poll = async () => {
    let status;
    try {
      status = await api('/api/publish');
    } catch {
      // The server going away is not worth a scary message; the edit is on disk.
      renderPublish({ state: 'idle', message: '' });
      return;
    }

    renderPublish(status);
    // 'published' and 'failed' are terminal but the server clears them itself
    // after a while, so keep polling until it does — that is what makes the
    // line disappear on its own rather than sticking around all session.
    if (['pending', 'publishing', 'published', 'failed'].includes(status.state)) {
      publishTimer = setTimeout(poll, 2000);
    }
  };

  poll();
}

/* Read straight through the archive without going back to the rail.
   Returns null when there is nowhere to go, rather than a nav of two dashes. */
function dayNav(date) {
  const at = state.days.findIndex((day) => day.date === date);
  if (at < 0 || state.days.length < 2) return null;

  /* state.days is newest-first, so the *next* index is the *older* day.
     Reversing these two lines is silent — nothing would look broken. */
  const older = state.days[at + 1];
  const newer = at > 0 ? state.days[at - 1] : null;

  const side = (neighbour, kind, label) => {
    const inner = [
      el('span', { className: 'day-nav-label', textContent: label }),
      el('span', {
        className: 'day-nav-date',
        // An em dash rather than nothing, so reaching either end of the archive
        // does not shift what is next to it.
        textContent: neighbour ? prettyDate(neighbour.date) : '—',
      }),
    ];

    if (!neighbour) return el('span', { className: `day-nav-side ${kind} end` }, inner);

    const button = el('button', { type: 'button', className: `day-nav-side ${kind}` }, inner);
    // Through showDay, which already guards unsaved notes — so this new way of
    // leaving a day inherits the prompt instead of needing its own.
    button.addEventListener('click', () => showDay(neighbour.date));
    return button;
  };

  const nav = el('nav', { className: 'day-nav' }, [
    side(older, 'older', '← Older'),
    side(newer, 'newer', 'Newer →'),
  ]);
  nav.setAttribute('aria-label', 'Nearby days');
  return nav;
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
  const chips = allTags.map((tag) => {
    const chip = el('button', { type: 'button', className: 'chip', textContent: tag });
    chip.setAttribute('aria-pressed', String(state.activeTag === tag));
    chip.addEventListener('click', () => {
      state.activeTag = state.activeTag === tag ? '' : tag;
      renderTags();
      closeDrawer();
      runSearch();
    });
    return chip;
  });

  // Alongside the topic chips rather than in its own panel: it answers the same
  // question they do — what am I looking at — just from the other side.
  const dropped = skippedForVisibleDays();
  if (dropped.length) {
    const chip = el('button', {
      type: 'button',
      className: 'chip skipped-chip',
      textContent: `skipped (${dropped.length})`,
      title: 'Topics left out as off topic. Tune the lists in config.toml.',
    });
    chip.setAttribute('aria-pressed', String(state.activeTag === SKIPPED));
    chip.addEventListener('click', () => {
      state.activeTag = state.activeTag === SKIPPED ? '' : SKIPPED;
      renderTags();
      closeDrawer();
      runSearch();
    });
    chips.push(chip);
  }

  $('tags').replaceChildren(...chips);

  if (!chips.length) {
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

  if (state.activeTag === SKIPPED) {
    showSkipped();
    return;
  }

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

function showSkipped() {
  const dropped = skippedForVisibleDays().filter((entry) => {
    const needle = state.query.trim().toLowerCase();
    return !needle
      || `${entry.headline} ${entry.reason}`.toLowerCase().includes(needle);
  });

  const heading = el('div', { className: 'day-head' }, [
    el('h2', { textContent: dropped.length
      ? `${dropped.length} left out as off topic`
      : 'Nothing left out' }),
    el('span', {
      className: 'day-meta',
      textContent: 'Judged outside your interests. Edit [interests] in config.toml to change this.',
    }),
  ]);

  if (!dropped.length) {
    $('main').replaceChildren(heading, el('p', {
      className: 'empty', textContent: 'The filter has dropped nothing yet.',
    }));
    return;
  }

  const list = el('ul', { className: 'skipped-list' },
    dropped.map(({ date, headline, reason }) => {
      const row = el('li', {}, [
        el('div', { className: 'skipped-headline', textContent: headline }),
        el('div', { className: 'hit-date', textContent: prettyDate(date, true) }),
        reason ? el('div', { className: 'why', textContent: reason }) : null,
      ]);

      if (!READ_ONLY) {
        const restore = el('button', {
          type: 'button',
          className: 'restore-topic',
          textContent: 'restore to the feed',
        });
        restore.addEventListener('click', () => restoreTopic(date, headline, restore));
        row.append(restore);
      }

      return row;
    }));

  $('main').replaceChildren(heading, list);
  window.scrollTo({ top: 0 });
}

function describeFilters() {
  const parts = [];
  if (state.query.trim()) parts.push(`“${state.query.trim()}”`);
  if (state.activeTag === SKIPPED) parts.push('left out as off topic');
  else if (state.activeTag) parts.push(`tagged ${state.activeTag}`);
  return parts.join(' · ');
}

function syncActiveFilter() {
  const button = $('active-filter');
  button.hidden = !state.activeTag;
  const label = state.activeTag === SKIPPED ? 'skipped' : state.activeTag;
  button.textContent = label;
  button.title = `Clear the ${label} filter`;
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

  // Shown so an over-aggressive interest filter is visible rather than silent.
  if (run.skipped && run.skipped.length) {
    detail.append(el('p', {
      className: 'status',
      textContent: `Left out as off topic (${run.skipped.length}):`,
    }));
    detail.append(el('ul', { className: 'skipped' },
      run.skipped.map((note) => el('li', { textContent: note }))));
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

const monthOf = (iso) => iso.slice(0, 7);      // "2026-07-29" → "2026-07"

function prettyMonth(month) {
  const [year, index] = month.split('-').map(Number);
  return new Date(year, index - 1, 1)
    .toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

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
  if (!READ_ONLY) {
    wireAddSource();
    // A push left in flight by a previous session should still be visible. This
    // stops after one request when there is nothing happening.
    watchPublish();
  }

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
    renderTags();               // the skipped chip needs the day list to exist

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
