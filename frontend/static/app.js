/*
 * TradePilot AI: the dashboard page.
 *
 * Shows what the API returns (docs/WEB.md): the snapshot of GET /api/state,
 * the candles of GET /api/candles and the "tick" events of GET /api/stream.
 * Signals only: nothing here places, changes or closes an order.
 *
 * Rules kept here:
 * - Server strings reach the page through textContent, never through innerHTML.
 * - Times arrive as UTC Unix seconds and are shown in the display zone
 *   (snapshot.utc_offset_hours) by shifting the timestamp and reading it with
 *   UTC getters. The browser's own time zone is never used.
 * - A price is shown as current only while ticks keep arriving and the feed
 *   is not down. Otherwise it is marked in words.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- constants

  const MINUS = '\u2212';
  const DASH = '\u2014';
  const EN_DASH = '\u2013';
  const NBSP = '\u00A0';
  const LOST_AFTER_MS = 10000; // no tick for this long: the page lost the server (see lostAfterMs)
  const REQUEST_TIMEOUT_MS = 15000; // a request without an answer for this long is given up
  const LEAVE_AFTER_MS = 1500; // between saying that the session ended and leaving for the login page
  const NOTIFY_LOST_AFTER_MS = 60000;
  const SILENT_STREAM_MS = 120000; // an "open" stream that stays silent this long is opened again
  const CANDLE_LIMIT = 1500;
  const CANDLE_PAUSE_MS = 3000;
  const SNAPSHOT_PAUSE_MS = 5000; // after a failed /api/state: asked again this much later
  const SNAPSHOT_FAILS_SHOWN = 3; // this many failed in a row: the page says so
  const ALERT_FLAG_MS = 60000; // how long "the feed is back" stays in the tab title
  const TAIL_BARS = 3; // newest candles asked for when the tick does not carry the chart's timeframe
  const TAIL_EVERY_MS = 1500;
  const TAIL_FAILS_SHOWN = 3;
  // Chart timeframes: seconds of the fixed ones, months of the calendar ones.
  const STEP = {
    M1: 60, M2: 120, M3: 180, M5: 300, M10: 600, M15: 900, M30: 1800, M45: 2700,
    H1: 3600, H2: 7200, H3: 10800, H4: 14400, D1: 86400, W1: 604800,
  };
  const MONTHS = { MN1: 1, MN3: 3, MN6: 6, MN12: 12 };
  const WEEK_OFFSET = 4 * 86400; // weeks open on Monday 00:00 UTC; second 0 was a Thursday
  const TF_GROUPS = [
    {
      title: 'Минут',
      items: [
        ['M1', '1 минут'], ['M2', '2 минут'], ['M3', '3 минут'], ['M5', '5 минут'],
        ['M10', '10 минут'], ['M15', '15 минут'], ['M30', '30 минут'], ['M45', '45 минут'],
      ],
    },
    { title: 'Цаг', items: [['H1', '1 цаг'], ['H2', '2 цаг'], ['H3', '3 цаг'], ['H4', '4 цаг']] },
    { title: 'Өдөр, долоо хоног', items: [['D1', '1 өдөр'], ['W1', '1 долоо хоног']] },
    { title: 'Сар', items: [['MN1', '1 сар'], ['MN3', '3 сар'], ['MN6', '6 сар'], ['MN12', '12 сар']] },
  ];
  const TF_ORDER = [];
  const TF_NAMES = {};
  for (const group of TF_GROUPS) {
    for (const item of group.items) {
      TF_ORDER.push(item[0]);
      TF_NAMES[item[0]] = item[1];
    }
  }
  const DEFAULT_FAVORITES = ['M15', 'H1'];
  const STORE = {
    notify: 'tradepilot.notify',
    sound: 'tradepilot.sound',
    signal: 'tradepilot.lastSignal',
    broken: 'tradepilot.notifyBroken', // '1': this browser granted notifications and could not show one
    tf: 'tradepilot.chart.timeframe',
    favorites: 'tradepilot.chart.favorites',
  };
  const MONO = '"JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace';
  const COLOR = {
    card: '#111A2E',
    raised: '#1C2945',
    border: '#24314F',
    grid: '#1A2540',
    dim: '#8494B0',
    accent: '#38BDF8',
    up: '#2DD4BF',
    down: '#FB7185',
    upSoft: 'rgba(45, 212, 191, 0.16)', // the body of a candle that is still forming
    downSoft: 'rgba(251, 113, 133, 0.16)',
  };
  // The forecast: the line and its band, full and dimmed. Dimmed is for a model whose
  // measured record is no better than "the price stays where it is".
  const FORECAST_COLOR = {
    line: '#F472B6',
    band: 'rgba(244, 114, 182, 0.40)',
    lineDim: 'rgba(244, 114, 182, 0.70)', // dimmer than the full line, still clearer than its band
    bandDim: 'rgba(244, 114, 182, 0.22)',
  };
  const FORECAST_TF = 'M15'; // the forecast exists on this timeframe only and is never converted
  const FORECAST_LATE_S = 120; // a forecast whose next candle is this long overdue is not shown
  const FORECAST_POINTS = 4; // the contract (docs/WEB.md): one point for each of the next four candles
  const FORECAST_FAR = 0.2; // a point further from the base price than this share of it: a broken block
  const FORECAST_SCOPE = 'open_market'; // the one kind of record this page has words for
  const MAX_TIME = 4102444800; // 2100-01-01: no real time lies beyond it (the server's own limit)

  const CONDITIONS = {
    trend_ema200: { long: 'Урт хугацааны чиг (EMA200)', short: 'EMA200' },
    trend_ema20_50: { long: 'Богино хугацааны чиг (EMA20 / EMA50)', short: 'EMA20 / EMA50' },
    macd_vs_signal: { long: 'MACD ба signal шугам', short: 'MACD ба signal' },
    macd_vs_zero: { long: 'MACD ба тэг шугам', short: 'MACD ба тэг' },
    rsi_zone: { long: 'RSI(14) бүс', short: 'RSI(14)' },
  };
  const BLOCK_TITLES = {
    HTF_CONFLICT: 'H1 чиг зөрсөн',
    MARKET_CLOSED: 'Зах зээл хаалттай',
    INSUFFICIENT_BARS: 'Лаа хүрэлцэхгүй',
    STALE_FEED: 'Ханш тасарсан',
    DUPLICATE: 'Ижил чиглэлийн сигнал хүчинтэй байгаа',
    NEWS_WINDOW: 'Мэдээний цонх',
  };
  const LEVEL_WORDS = { ok: 'Хэвийн', warn: 'Анхаарах', down: 'Зогссон' };
  const SOURCE_NAMES = { binance: 'Binance', twelvedata: 'Twelve Data', csv: 'CSV' };
  const STATUS_WORDS = {
    ok: 'Төлөв: хэвийн.',
    late: 'Төлөв: анхаарах. Ханш хоцорч байна.',
    down: 'Төлөв: зогссон. Ханш тасарсан.',
    lost: 'Сервертэй холбоо тасарсан.',
  };

  // -------------------------------------------------------------------- state

  const state = {
    snap: null, // the last answer of /api/state
    snapV: null, // its version
    tick: null, // the newest tick: from the stream, or the tick part of a newer snapshot
    tickWall: 0, // Date.now() when it arrived
    tickMono: 0, // performance.now() at the same moment
    bootWall: Date.now(),
    bootMono: performance.now(),
    offset: 8, // display zone, hours ahead of UTC
    symbol: 'XAUUSD',
    timeframe: 'M15',
    source: { name: '', symbol: '' },
    lost: false, // no tick for LOST_AFTER_MS
    lostSince: 0,
    reconnecting: false, // the stream reported an error and has not delivered since
    leaving: false,
    leaveTimer: null, // the pause before the page leaves for the login page (goLogin)
    statusKind: null,
  };

  const view = {
    cardKey: null,
    cardChip: null,
    cardChipShown: null,
    cardUpdate: null,
    cardSaid: null,
    notesKey: null,
    reasonsKey: null,
    logKey: null,
    alertsKey: null,
    flagKey: null,
    chartNoteKey: null,
    forecastKey: null,
  };

  const chart = {
    api: null,
    series: null,
    markers: null,
    tf: storedTimeframe(), // the timeframe the user picked: a way of looking, signals do not depend on it
    loadedTf: null, // the timeframe whose candles are in the series
    bars: [], // those candles, times in UTC seconds; closed: false marks the forming one
    index: new Map(), // time -> position in bars
    last: {}, // last bar time per timeframe
    favorites: storedFavorites(), // the starred timeframes: quick buttons above the chart
    supported: null, // the timeframes the server can show; null until a snapshot says
    info: null, // what the last full load said about the history: {history, partialFirst, gaps}
    tailAt: 0,
    tailBusy: false,
    tailFails: 0,
    menuOpen: false,
    shift: 8 * 3600, // seconds added to every time handed to the chart
    request: 0,
    loading: false,
    startedAt: 0,
    stale: false, // a candle on screen missed its close: load again until one load succeeds
    lines: [],
    linesKey: '',
    levels: null, // {min, max} of the price lines, kept inside the visible price range
    hover: null,
    forecast: null, // {line, low, high, slots, label}: the forecast's series and its label on the chart
    forecastKey: '', // what the three drawn series hold now; '' when they are empty
    slotsKey: '', // which future candles the time axis reaches to; '' when none
  };

  const stream = { source: null, broken: false, timer: null, delay: 2000, restartedAt: 0 };

  // fails: /api/state requests failed in a row; timer: the retry after a failed one
  const snapshotJob = { busy: false, queued: false, retryAt: 0, fails: 0, timer: null };

  const notif = {
    enabled: readStore(STORE.notify) !== '0',
    sound: readStore(STORE.sound) !== '0',
    interacted: false,
    baseline: false,
    lastSignal: 0,
    lastAlert: 0,
    audio: null,
    titleBase: document.title,
    signalFlag: null, // {text, time, until}: a new signal's word for the tab title, until it expires
    alertFlag: null, // {text, down, wall, mono}: a feed alert's word for the tab title
    // The browser allowed notifications and then refused to show one. Kept across visits:
    // otherwise the button would say "on" again until the next signal was missed.
    broken: readStore(STORE.broken) === '1',
    lostSaid: false,
  };

  // ------------------------------------------------------------ small helpers

  function $(id) {
    return document.getElementById(id);
  }

  function isNum(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  /* A value of a lookup table by a key that may come from the server: own keys only. */
  function pick(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
  }

  function safe(fn) {
    try {
      fn();
    } catch (error) {
      if (window.console && console.error) {
        console.error(error);
      }
    }
  }

  function readStore(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null; // the page works without storage
    }
  }

  function writeStore(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (error) {
      /* the page works without storage */
    }
  }

  // Chart timeframes. The boundaries are the server's (core/candles.py): UTC, weeks
  // from Monday, month timeframes by the calendar.

  function knownTf(tf) {
    return typeof tf === 'string' && (pick(STEP, tf) !== undefined || pick(MONTHS, tf) !== undefined);
  }

  /* Open time of the tf candle a moment falls in. */
  function candleOpen(time, tf) {
    const months = pick(MONTHS, tf);
    if (months !== undefined) {
      const d = new Date(time * 1000);
      const index = d.getUTCFullYear() * 12 + d.getUTCMonth();
      const start = Math.floor(index / months) * months;
      return Date.UTC(Math.floor(start / 12), start % 12, 1) / 1000;
    }
    const step = STEP[tf];
    const offset = tf === 'W1' ? WEEK_OFFSET : 0;
    return Math.floor((time - offset) / step) * step + offset;
  }

  /* Open time of the candle after the one that opens at `open`. */
  function nextOpen(open, tf) {
    const months = pick(MONTHS, tf);
    if (months !== undefined) {
      const d = new Date(open * 1000);
      const index = d.getUTCFullYear() * 12 + d.getUTCMonth() + months;
      return Date.UTC(Math.floor(index / 12), index % 12, 1) / 1000;
    }
    return open + STEP[tf];
  }

  function storedTimeframe() {
    const kept = readStore(STORE.tf);
    return knownTf(kept) ? kept : 'M15';
  }

  function storedFavorites() {
    let kept = null;
    try {
      kept = JSON.parse(readStore(STORE.favorites));
    } catch (error) {
      kept = null;
    }
    if (!Array.isArray(kept)) {
      return DEFAULT_FAVORITES.slice();
    }
    return TF_ORDER.filter(function (tf) {
      return kept.indexOf(tf) !== -1;
    });
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function setText(node, text) {
    if (node && node.textContent !== text) {
      node.textContent = text;
    }
  }

  function show(node, on) {
    if (node && node.hidden === on) {
      node.hidden = !on;
    }
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const CIRCLE = 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z';
  const ICONS = {
    up: ['M12 19V5', 'M5 12l7-7 7 7'],
    down: ['M12 5v14', 'M19 12l-7 7-7-7'],
    clock: [CIRCLE, 'M12 7v5l3 2'],
    check: ['M5 12l5 5 9-10'],
    pause: ['M9 6v12', 'M15 6v12'],
    warn: ['M12 4l9 16H3l9-16z', 'M12 10v4', 'M12 17h.01'],
    stop: [CIRCLE, 'M5.6 5.6l12.8 12.8'],
    bell: ['M6 16v-5a6 6 0 0 1 12 0v5l2 2H4l2-2z', 'M10 21h4'],
    bellOff: ['M6 16v-5a6 6 0 0 1 12 0v5l2 2H4l2-2z', 'M10 21h4', 'M3 3l18 18'],
    sound: ['M4 9v6h4l5 4V5L8 9H4z', 'M16.5 8.5a5 5 0 0 1 0 7', 'M19 6a8.5 8.5 0 0 1 0 12'],
    mute: ['M4 9v6h4l5 4V5L8 9H4z', 'M17 9.5l4 5', 'M21 9.5l-4 5'],
    star: ['M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.8z'],
  };

  function icon(name, size, weight) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', String(weight || 2));
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    for (const d of pick(ICONS, name) || []) {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    }
    return svg;
  }

  function announce(text) {
    const live = $('sr-live');
    if (!live) {
      return;
    }
    live.textContent = '';
    window.setTimeout(function () {
      live.textContent = text;
    }, 50);
  }

  // ---------------------------------------------------------- time and numbers

  function shifted(seconds) {
    return new Date((seconds + state.offset * 3600) * 1000);
  }

  function fmtClock(seconds) {
    if (!isNum(seconds)) {
      return DASH;
    }
    const d = shifted(seconds);
    return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  function fmtDay(seconds) {
    const d = shifted(seconds);
    return pad2(d.getUTCMonth() + 1) + '.' + pad2(d.getUTCDate());
  }

  function fmtDateTime(seconds) {
    const d = shifted(seconds);
    return d.getUTCFullYear() + '.' + fmtDay(seconds) + ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  function dayIndex(seconds) {
    return Math.floor((seconds + state.offset * 3600) / 86400);
  }

  /* "16:30", with the date in front when the moment is not on the same day as ``now``. */
  function fmtStamp(seconds, now) {
    if (!isNum(seconds)) {
      return DASH;
    }
    if (isNum(now) && dayIndex(seconds) !== dayIndex(now)) {
      return fmtDay(seconds) + ' ' + fmtClock(seconds);
    }
    return fmtClock(seconds);
  }

  function fmtPrice(value) {
    return isNum(value) ? value.toFixed(2) : DASH;
  }

  function fmtSigned(value, digits) {
    if (!isNum(value)) {
      return DASH;
    }
    const body = Math.abs(value).toFixed(digits === undefined ? 2 : digits);
    return (value < 0 && Number(body) !== 0 ? MINUS : '+') + body;
  }

  function fmtScore(value) {
    if (!isNum(value)) {
      return DASH;
    }
    if (value === 0) {
      return '0';
    }
    return (value < 0 ? MINUS : '+') + Math.abs(value);
  }

  function toneOf(value) {
    if (!isNum(value) || value === 0) {
      return 'zero';
    }
    return value > 0 ? 'up' : 'down';
  }

  function countdownText(left) {
    if (!(left > 0)) {
      return 'Хугацаа дууссан';
    }
    if (left < 60) {
      return Math.max(1, Math.floor(left)) + ' сек үлдсэн';
    }
    return Math.floor(left / 60) + ' мин үлдсэн';
  }

  function agoText(seconds) {
    const value = Math.max(0, Math.round(seconds));
    return value < 120 ? value + ' сек өмнө' : Math.floor(value / 60) + ' мин өмнө';
  }

  function sourceName() {
    const name = state.source.name;
    if (!name) {
      return 'Эх үүсвэр';
    }
    return pick(SOURCE_NAMES, name) || name.charAt(0).toUpperCase() + name.slice(1);
  }

  function sourceFull() {
    if (!state.source.name) {
      return DASH;
    }
    return state.source.symbol ? sourceName() + ' ' + state.source.symbol : sourceName();
  }

  function experimental() {
    const rules = state.snap && state.snap.rules;
    return !rules || rules.status === 'experimental';
  }

  function candleLine(closeTime, now) {
    const head = state.symbol + ' · ' + state.timeframe;
    if (!isNum(closeTime) || closeTime <= 0) {
      return head;
    }
    return head + ' · ' + fmtStamp(closeTime, now) + '-д хаагдсан лаа';
  }

  function nextCheckText(nextCheck, now) {
    if (isNum(nextCheck) && isNum(now) && nextCheck > now) {
      return fmtStamp(nextCheck, now) + '-д дараагийн лаа хаагдахад дахин шалгана.';
    }
    return 'Дараагийн лаа хаагдахад дахин шалгана.';
  }

  function idleMs() {
    if (state.tick) {
      return Math.max(Date.now() - state.tickWall, performance.now() - state.tickMono);
    }
    return Math.max(Date.now() - state.bootWall, performance.now() - state.bootMono);
  }

  /* The server's clock, carried forward from the last tick by the time that passed here. */
  function estimatedNow() {
    if (!state.tick || !isNum(state.tick.now)) {
      return null;
    }
    return state.tick.now + Math.max(0, Date.now() - state.tickWall) / 1000;
  }

  // ------------------------------------------------------------------ network

  class AuthError extends Error {}

  function goLogin() {
    if (state.leaving) {
      return;
    }
    state.leaving = true;
    closeStream();
    if (state.snap === null) {
      window.location.assign('/login'); // never got in: nothing was running that could stop
      return;
    }
    // The session ran out under an open dashboard. From here on this page announces
    // nothing, so it says so first: a notification, the alert sound, and the login
    // page's own title and message.
    safe(function () {
      pushNotice(
        'TradePilot · Нэвтрэх хугацаа дууссан',
        'Мэдэгдэл зогслоо. Дахин нэвтэртэл шинэ сигнал энэ хөтчид мэдэгдэхгүй.',
        'tradepilot-session'
      );
      beep('alert');
    });
    state.leaveTimer = window.setTimeout(function () {
      state.leaveTimer = null;
      window.location.assign('/login?expired=1');
    }, LEAVE_AFTER_MS);
  }

  async function request(path, options) {
    const settings = Object.assign(
      { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } },
      options || {}
    );
    const response = await fetch(path, settings);
    if (response.status === 401) {
      goLogin();
      throw new AuthError('login required');
    }
    return response;
  }

  /*
   * GET and parse. A request or a body that stalls is given up after REQUEST_TIMEOUT_MS and
   * fails like any other, so nothing that waits for it (the next snapshot, the next candle
   * load) is blocked for good. The time limit does not depend on the browser honouring the abort.
   */
  async function getJSON(path) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timer = null;
    const limit = new Promise(function (resolve, reject) {
      timer = window.setTimeout(function () {
        if (controller) {
          controller.abort();
        }
        reject(new Error('timeout'));
      }, REQUEST_TIMEOUT_MS);
    });
    const answer = (async function () {
      const response = await request(path, controller ? { signal: controller.signal } : undefined);
      if (!response.ok) {
        throw new Error('HTTP ' + response.status);
      }
      return response.json();
    })();
    answer.catch(function () {
      /* a failure that comes after the time limit was already reported as a timeout */
    });
    try {
      return await Promise.race([answer, limit]);
    } finally {
      window.clearTimeout(timer);
    }
  }

  /*
   * Fetch /api/state. Never two at once: a change that arrives while one is
   * on its way is answered by one more fetch afterwards, and only if the
   * snapshot that came back is still not the version the stream reports.
   */
  async function refreshSnapshot() {
    if (state.leaving) {
      return;
    }
    if (snapshotJob.busy) {
      snapshotJob.queued = true;
      return;
    }
    snapshotJob.busy = true;
    if (snapshotJob.timer !== null) {
      window.clearTimeout(snapshotJob.timer);
      snapshotJob.timer = null;
    }
    try {
      for (let round = 0; round < 2; round += 1) {
        snapshotJob.queued = false;
        const snap = await getJSON('/api/state');
        snapshotJob.retryAt = 0;
        snapshotJob.fails = 0;
        applySnapshot(snap);
        if (!snapshotJob.queued || !state.tick || state.tick.v === state.snapV) {
          break;
        }
      }
    } catch (error) {
      snapshotJob.retryAt = Date.now() + SNAPSHOT_PAUSE_MS; // the ticks ask again, but not every two seconds
      if (!(error instanceof AuthError)) {
        snapshotJob.fails += 1;
        if (window.console && console.warn) {
          console.warn('snapshot:', error);
        }
      }
    } finally {
      snapshotJob.busy = false;
      snapshotJob.queued = false;
    }
    if (snapshotJob.fails > 0 && !state.leaving) {
      // Asked again by a timer, not by the next tick: with a slow poll that tick is far away,
      // and without a stream it never comes.
      snapshotJob.timer = window.setTimeout(function () {
        snapshotJob.timer = null;
        refreshSnapshot();
      }, SNAPSHOT_PAUSE_MS);
      safe(checkTickSignal); // no snapshot to be had: a new signal is announced from the tick's own card
    }
    safe(renderCardNotes); // says so after a few failures in a row, and stops saying so after a success
  }

  function applySnapshot(snap) {
    if (!snap || typeof snap !== 'object') {
      return;
    }
    state.snap = snap;
    state.snapV = snap.v;
    if (isNum(snap.utc_offset_hours)) {
      state.offset = snap.utc_offset_hours;
    }
    if (typeof snap.symbol === 'string' && snap.symbol) {
      state.symbol = snap.symbol;
    }
    if (typeof snap.timeframe === 'string' && snap.timeframe) {
      state.timeframe = snap.timeframe;
    }
    if (snap.source && typeof snap.source === 'object') {
      state.source = { name: String(snap.source.name || ''), symbol: String(snap.source.symbol || '') };
    }

    // The snapshot starts with a tick of its own. Use it unless the stream already brought a newer one.
    if (!state.tick || !isNum(state.tick.now) || (isNum(snap.now) && snap.now >= state.tick.now)) {
      takeTick(snap, false);
    } else {
      renderLive();
    }
    renderSnapshotParts();

    if (chart.series && chart.loadedTf && chart.shift !== state.offset * 3600) {
      loadCandles(false); // the display zone changed: the chart's times must move with it
    }
    safe(function () {
      checkNotifications(snap);
    });
  }

  function takeTick(tick, fromStream) {
    state.tick = tick;
    state.tickWall = Date.now();
    state.tickMono = performance.now();
    if (fromStream) {
      state.reconnecting = false;
    }
    if (state.lost) {
      state.lost = false;
      state.lostSince = 0;
      if (notif.lostSaid) {
        notif.lostSaid = false;
        pushNotice('TradePilot · Холбоо сэргэлээ', 'Хуудас сервертэй дахин холбогдлоо.', 'tradepilot-link');
      }
    }
    renderLive();
    safe(function () {
      applyBars(tick.bars);
    });
    safe(syncPriceLines);
    safe(syncForecast); // a candle may have closed: the forecast made before it must go
  }

  function onTick(tick) {
    if (!tick || typeof tick !== 'object') {
      return;
    }
    takeTick(tick, true);
    if (tick.v !== state.snapV && Date.now() >= snapshotJob.retryAt) {
      refreshSnapshot(); // the snapshot changed: reasons, log, tiles, alerts, markers
    }
    safe(checkTickSignal);
  }

  // The stream. EventSource reconnects by itself; a new one is made only when the old one is closed.

  function openStream() {
    if (state.leaving || typeof window.EventSource !== 'function') {
      return;
    }
    if (stream.source && stream.source.readyState !== window.EventSource.CLOSED) {
      return;
    }
    let source;
    try {
      source = new window.EventSource('/api/stream');
    } catch (error) {
      scheduleReopen();
      return;
    }
    stream.source = source;

    source.addEventListener('open', function () {
      if (stream.source !== source) {
        return;
      }
      stream.delay = 2000;
      if (stream.broken) {
        stream.broken = false;
        requestCandles(); // ticks were missed: load the candles again
        refreshSnapshot();
      }
    });

    source.addEventListener('tick', function (event) {
      if (stream.source !== source) {
        return;
      }
      let tick;
      try {
        tick = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      onTick(tick);
    });

    source.addEventListener('error', function () {
      if (stream.source !== source) {
        return;
      }
      stream.broken = true;
      state.reconnecting = true;
      safe(renderStatus);
      if (source.readyState === window.EventSource.CLOSED) {
        scheduleReopen(); // the browser gave up (a 401 or another error answer)
      }
    });
  }

  function closeStream() {
    if (stream.timer !== null) {
      window.clearTimeout(stream.timer);
      stream.timer = null;
    }
    if (stream.source) {
      stream.source.close();
      stream.source = null;
    }
  }

  function scheduleReopen() {
    if (stream.timer !== null || state.leaving) {
      return;
    }
    stream.timer = window.setTimeout(async function () {
      stream.timer = null;
      await refreshSnapshot(); // goes to /login when the session has ended
      openStream();
    }, stream.delay);
    stream.delay = Math.min(stream.delay * 2, 15000);
  }

  /* Close a stream that looks open but delivers nothing, then open one again. */
  function restartStream() {
    if (state.leaving) {
      return;
    }
    stream.restartedAt = Date.now();
    closeStream();
    stream.broken = true;
    state.reconnecting = true;
    openStream();
  }

  function checkStream() {
    if (state.leaving) {
      return;
    }
    const source = stream.source;
    if (!source || source.readyState === window.EventSource.CLOSED) {
      if (stream.timer === null) {
        stream.broken = true;
        openStream();
      }
    } else if (state.lost && source.readyState === window.EventSource.OPEN && Date.now() - stream.restartedAt > 15000) {
      restartStream();
    }
  }

  /* Ticks come once per poll interval, so "no tick" means lost only after a few of those. */
  function lostAfterMs() {
    // The interval comes with every tick, so the limit is right even when no snapshot could be had.
    let poll = 0;
    if (state.tick && isNum(state.tick.poll_seconds)) {
      poll = state.tick.poll_seconds;
    } else if (state.snap && isNum(state.snap.poll_seconds)) {
      poll = state.snap.poll_seconds;
    }
    return Math.max(LOST_AFTER_MS, 3 * poll * 1000);
  }

  function watchdog() {
    const idle = idleMs();
    if (idle > lostAfterMs()) {
      if (!state.lost) {
        state.lost = true;
        state.lostSince = Date.now();
        renderLive();
      } else {
        safe(renderStatus); // the seconds in the pill keep counting
      }
      safe(syncForecast); // no forecast is shown over candles that no longer move
      if (state.tick && !notif.lostSaid && Date.now() - state.lostSince > NOTIFY_LOST_AFTER_MS) {
        notif.lostSaid = true;
        pushNotice(
          'TradePilot · Холбоо тасарсан',
          'Хуудас сервертэй холбогдож чадахгүй байна. Шинэ сигнал энд харагдахгүй.',
          'tradepilot-link'
        );
        beep('alert');
      }
      const source = stream.source;
      if (!source || source.readyState === window.EventSource.CLOSED) {
        checkStream();
      } else if (
        source.readyState === window.EventSource.OPEN &&
        idle > SILENT_STREAM_MS &&
        Date.now() - stream.restartedAt > SILENT_STREAM_MS
      ) {
        restartStream();
      }
    }
    if (chart.series && chart.loadedTf !== chart.tf) {
      requestCandles(); // a load of the candles failed: keep trying
    }
    safe(renderClock);
    safe(paintTitle); // a flag in the tab title ends by the clock, with or without ticks
  }

  // ------------------------------------------------------- live parts (ticks)

  function renderLive() {
    safe(renderStatus);
    safe(renderClock);
    safe(renderPrice);
    safe(renderCard);
    safe(renderChartNote);
    safe(paintTitle);
  }

  function renderStatus() {
    const pill = $('status-pill');
    const text = $('status-text');
    const tick = state.tick;
    let kind = 'starting';
    let label = 'Ачаалж байна';

    if (state.lost) {
      kind = 'lost';
      label = tick
        ? 'Сервертэй холбоо тасарсан · ' + agoText(idleMs() / 1000)
        : 'Сервертэй холбоо тасарсан';
    } else if (!tick) {
      kind = 'starting';
    } else if (state.reconnecting) {
      kind = 'wait';
      label = 'Дахин холбогдож байна';
    } else if (tick.feed === 'ok') {
      kind = 'ok';
      label = 'Хэвийн · ханш ' + Math.max(0, Math.round(tick.age || 0)) + ' сек өмнө';
    } else if (tick.feed === 'late') {
      kind = 'late';
      label = 'Анхаарах · ханш ' + Math.max(0, Math.round(tick.age || 0)) + ' сек өмнө';
    } else if (tick.feed === 'down') {
      kind = 'down';
      label = 'Зогссон · ханш ' + Math.max(1, Math.floor((tick.age || 0) / 60)) + ' мин өмнө';
    }

    const className = 'status-pill is-' + kind;
    if (pill.className !== className) {
      pill.className = className;
    }
    setText(text, label);

    if (kind !== state.statusKind) {
      const before = state.statusKind;
      state.statusKind = kind;
      if (before !== null && pick(STATUS_WORDS, kind) && before !== 'starting') {
        announce(pick(STATUS_WORDS, kind));
      }
    }
  }

  function renderClock() {
    const now = estimatedNow();
    const node = $('clock');
    if (now === null) {
      setText(node, DASH);
      return;
    }
    const text = fmtDateTime(now);
    if (node.textContent !== text) {
      node.textContent = text;
      node.setAttribute('datetime', new Date(Math.floor(now / 60) * 60000).toISOString());
    }
    setText($('clock-zone'), state.offset === 8 ? 'Улаанбаатар' : 'UTC' + fmtSigned(state.offset, 0));
  }

  function renderPrice() {
    const tick = state.tick;
    const tile = $('price-tile');
    const flag = $('price-flag');
    if (!tick) {
      return;
    }
    setText($('price-value'), fmtPrice(tick.price));

    const quote = $('price-quote');
    if (isNum(tick.bid) && isNum(tick.ask)) {
      setText($('price-bid'), 'Bid ' + fmtPrice(tick.bid));
      setText($('price-ask'), 'Ask ' + fmtPrice(tick.ask));
      show(quote, true);
    } else {
      show(quote, false);
    }

    // Never an old price without saying so.
    let kind = '';
    let words = '';
    if (isNum(tick.price)) {
      if (state.lost) {
        kind = 'down';
        words = 'Одоогийн ханш биш · сүүлд ' + fmtStamp(tick.now, estimatedNow());
      } else if (tick.feed === 'down') {
        const card = tick.card || {};
        const at = isNum(card.last_time) ? card.last_time : tick.now - (tick.age || 0);
        kind = 'down';
        words = 'Одоогийн ханш биш · сүүлд ' + fmtStamp(at, tick.now);
      } else if (tick.feed === 'late') {
        kind = 'warn';
        words = Math.round(tick.age || 0) + ' сек өмнөх ханш';
      }
    }
    tile.classList.toggle('is-stale', kind === 'down');
    const key = kind + '|' + words;
    if (key !== view.flagKey) {
      view.flagKey = key;
      if (!kind) {
        flag.replaceChildren();
        flag.hidden = true;
      } else {
        flag.className = 'flag flag-' + kind;
        flag.replaceChildren(icon('warn', 13, 2.2), el('span', null, words));
        flag.hidden = false;
      }
    }
  }

  /* What the reader must know about the candles on screen: one note per fact, the worst first. */
  function renderChartNote() {
    const host = $('chart-notes');
    const tick = state.tick;
    const notes = []; // [kind, icon, words]
    let broken = true;
    if (state.lost && tick) {
      notes.push(['down', 'warn', 'Сервертэй холбоо тасарсан. График шинэчлэгдэхгүй байна.']);
    } else if (tick && tick.feed === 'down') {
      notes.push(['down', 'warn', 'Ханш тасарсан. Сүүлийн лаа шинэчлэгдэхгүй байна.']);
    } else if (chart.tailFails >= TAIL_FAILS_SHOWN) {
      notes.push([
        'down',
        'warn',
        chart.tf + ' лааг эх үүсвэрээс авч чадахгүй байна. Сүүлийн лаа шинэчлэгдэхгүй байна.',
      ]);
    } else {
      broken = false;
    }
    const info = chart.loadedTf === chart.tf ? chart.info : null;
    if (info && info.gaps > 0) {
      notes.push([
        'warn',
        'warn',
        'Энэ хугацааны өгөгдөл ' + info.gaps + ' газарт тасарсан. Тасарсан хэсэгт лаа зураагүй.',
      ]);
    }
    if (info && info.history === 'short') {
      notes.push([
        'warn',
        'clock',
        'Түүх богино: эх үүсвэрт ' + chart.tf + ' хугацааны ердөө ' + chart.bars.length + ' лаа байна.' +
          (info.partialFirst ? ' Эхний лаа бүтэн биш: түүх тэр хугацааны дундаас эхэлсэн.' : ''),
      ]);
    }
    if (!broken && tick && tick.market_closed === true && state.source.name) {
      // The source trades on while the broker is closed: say whose candles these are.
      notes.push([
        'warn',
        'clock',
        'Брокерын зах зээл хаалттай байж магадгүй. Энэ үеийн лаа нь ' +
          sourceFull() +
          '-ийн бодит арилжааны ханш (амралтын өдөр ч арилжаалагддаг), брокерын ханш биш.',
      ]);
    }
    const key = notes
      .map(function (note) {
        return note[0] + note[2];
      })
      .join('|');
    if (key === view.chartNoteKey) {
      return;
    }
    view.chartNoteKey = key;
    host.replaceChildren.apply(
      host,
      notes.map(function (note) {
        return noteNode(note[0], note[1], note[2]);
      })
    );
    host.hidden = notes.length === 0;
  }

  // -------------------------------------------------------------- signal card

  function chipNode(text, variant, iconName, title) {
    const chip = el('div', variant ? 'chip chip-' + variant : 'chip');
    if (iconName) {
      chip.appendChild(icon(iconName, 14, 2));
    }
    const label = el('span', null, text);
    chip.appendChild(label);
    if (title) {
      chip.title = title;
    }
    chip.label = label;
    return chip;
  }

  function sideRow(iconName, tone, title, isSide, subNode) {
    const row = el('div', 'side-row');
    const box = el('div', 'side-icon tone-' + tone);
    box.setAttribute('aria-hidden', 'true');
    box.appendChild(icon(iconName, 28, isSide || iconName === 'pause' ? 2.4 : 2));
    const text = el('div', 'side-text');
    text.appendChild(el('div', 'side-title tone-' + tone + (isSide ? ' is-side' : ''), title));
    if (subNode) {
      text.appendChild(subNode);
    }
    row.append(box, text);
    return row;
  }

  function boxNode(children, extraClass) {
    const box = el('div', extraClass ? 'box ' + extraClass : 'box');
    box.append.apply(box, children);
    return box;
  }

  function footNode(lines) {
    const foot = el('div', 'card-foot');
    for (const line of lines) {
      foot.appendChild(typeof line === 'string' ? el('div', null, line) : line);
    }
    return foot;
  }

  function condNode(condition, short) {
    const names = pick(CONDITIONS, condition.id);
    const node = el('span', short ? 'cond is-short' : 'cond');
    node.appendChild(el('span', null, names ? (short ? names.short : names.long) : String(condition.id)));
    node.appendChild(el('span', 'cond-value val-' + toneOf(condition.value), fmtScore(condition.value)));
    return node;
  }

  function metaText(reasons) {
    return (
      'H1 оноо ' + fmtScore(reasons.h1_score) +
      ' · RSI ' + (isNum(reasons.rsi) ? reasons.rsi.toFixed(1) : DASH) +
      ' · ATR ' + fmtPrice(reasons.atr)
    );
  }

  /* The reasons of the snapshot, when they belong to the candle this card is about. */
  function reasonsFor(card) {
    const reasons = state.snap && state.snap.reasons;
    if (reasons && reasons.candle_close === card.candle_close && Array.isArray(reasons.conditions)) {
      return reasons;
    }
    return null;
  }

  function buildSignal(card) {
    const buy = card.side === 'BUY';
    const dir = buy ? 1 : -1;
    const outside = card.state === 'outside_zone';
    const src = sourceName();
    const until = fmtClock(card.expires_at);
    const slText = fmtSigned(-dir * card.sl_distance);
    const tpText = fmtSigned(dir * card.tp_distance);
    const zoneText = fmtPrice(card.zone_low) + ' ' + EN_DASH + ' ' + fmtPrice(card.zone_high);
    const sideName = String(card.side);

    const sub = el('div', 'side-sub');
    const nodes = [sideRow(buy ? 'up' : 'down', outside ? 'muted' : buy ? 'up' : 'down', sideName, true, sub)];

    const fill = el('div', outside ? 'bar-fill is-warn' : 'bar-fill');
    const bar = el('div', 'bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', 'Хүчинтэй хугацаа');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.appendChild(fill);
    const untilNode = el('span', 'mono', until + ' хүртэл');
    const validityHead = el('div', 'validity-head');
    validityHead.append(el('span', null, 'Хүчинтэй хугацаа'), untilNode);
    const validity = el('div', 'validity');
    validity.append(validityHead, bar);

    let chip;
    const priceNode = el('span');
    const ageNode = el('span', 'zone-age');

    if (outside) {
      chip = chipNode('Бүсээс гарсан', 'soft-warn');
      const where = el('div', 'meta-line');
      where.append(priceNode);
      nodes.push(
        boxNode([
          el('div', 'box-strong', 'Ханш орох бүсээс гарсан. Хөөж орохгүй.'),
          el('div', 'box-text', 'Ханш бүсэд буцаж орвол ' + until + ' хүртэл хүчинтэй.'),
          where,
        ]),
        validity,
        footNode(['Хугацаа дуусвал энэ сигнал хүчингүй болно.', 'Санхүүгийн зөвлөгөө биш. Шийдвэрээ өөрөө гаргана.'])
      );
    } else {
      chip = chipNode('', 'up', 'clock');

      const chips = el('div', 'chips');
      const grade = String(card.grade === undefined || card.grade === null ? DASH : card.grade);
      chips.appendChild(chipNode(grade + ' зэрэглэл', '', null, grade === 'B' ? 'AI нэгтгэл байхгүй тул B' : ''));
      if (experimental()) {
        chips.appendChild(chipNode('Туршилтын сигнал', 'warn'));
      }
      if (isNum(card.run) && card.run > 1) {
        chips.appendChild(
          chipNode(
            'Дараалсан ' + card.run + ' дахь ' + sideName,
            '',
            null,
            'Ижил чиглэлийн сигнал дараалан ' + card.run + ' дахь удаагаа гарч байна.'
          )
        );
      }
      nodes.push(chips);

      const zoneState = el('div', 'zone-state is-in');
      zoneState.append(icon('check', 14, 2.6), priceNode, ageNode);
      nodes.push(
        boxNode([
          el('div', 'box-label', 'Орох бүс · ' + src + ' ханшаар'),
          el('div', 'big-value', zoneText),
          zoneState,
        ])
      );

      const levels = el('div', 'levels');
      levels.append(
        levelBox('SL', 'Алдагдал зогсоох', 'down', slText, src, card.sl),
        levelBox('TP', 'Ашиг авах', 'up', tpText, src, card.tp)
      );
      nodes.push(levels, validity);

      const howto = el('div', 'howto');
      const steps = el('ol', 'steps');
      steps.append(
        el(
          'li',
          null,
          'Ханш бүс дотор эсэхийг эндээс харна. Эндэх түвшнүүд ' + src + '-ийн ханшаар тул MT5-ийн ханштай шууд тулгахгүй.'
        ),
        el('li', null, 'SL, TP-г өөрийн орсон үнээс зайгаар нь тавина: SL' + NBSP + slText + ', TP' + NBSP + tpText + '.'),
        el('li', null, 'Ханш бүсээс гарсан эсвэл хугацаа дууссан бол хөөж орохгүй.')
      );
      howto.append(el('h3', 'label', 'Орох бол'), steps);
      nodes.push(howto, footNode(['Санхүүгийн зөвлөгөө биш. Шийдвэрээ өөрөө гаргана.']));
    }

    return {
      nodes: nodes,
      chip: chip,
      said: outside
        ? sideName + ' сигнал. Ханш орох бүсээс гарсан.'
        : sideName + ' сигнал. ' + until + ' хүртэл хүчинтэй.',
      update: function (tick) {
        const left = card.expires_at - tick.now;
        const words = countdownText(left);
        const share = card.valid_seconds > 0 ? Math.min(100, Math.max(0, (left / card.valid_seconds) * 100)) : 0;
        fill.style.width = share.toFixed(1) + '%';
        bar.setAttribute('aria-valuenow', String(Math.round(share)));
        bar.setAttribute('aria-valuetext', words);
        setText(sub, candleLine(card.time, tick.now));
        if (outside) {
          setText(untilNode, until + ' хүртэл · ' + words);
          setText(priceNode, 'Орох бүс ' + zoneText + ' · ханш ' + fmtPrice(tick.price));
        } else {
          setText(chip.label, words);
          setText(priceNode, isNum(tick.price) ? 'Ханш бүс дотор: ' + fmtPrice(tick.price) : 'Ханш ирээгүй байна');
          setText(ageNode, tick.feed === 'late' ? '· ' + Math.round(tick.age || 0) + ' сек өмнөх ханш' : '');
        }
      },
    };
  }

  function levelBox(name, title, tone, distance, src, level) {
    const head = el('div', 'level-head');
    const label = el('span', 'level-name tone-' + tone, name);
    label.title = title;
    head.append(label, el('span', 'level-from', 'орсон үнээс'));
    const ref = el('div', 'level-ref');
    ref.append(src + ' ', el('span', 'mono', fmtPrice(level)));
    return boxNode([head, el('div', 'level-value', distance), ref], 'level');
  }

  function buildHold(card) {
    const sub = el('div', 'side-sub');
    const next = el('div');
    const nodes = [
      sideRow('pause', 'muted', 'Сигнал алга', false, sub),
      boxNode([
        el('div', 'box-label', 'Оноо'),
        el('div', 'big-value', fmtScore(card.score) + ' / 5'),
        el('div', 'box-text', 'BUY-д +3 ба түүнээс дээш, SELL-д ' + MINUS + '3 ба түүнээс доош оноо хэрэгтэй.'),
      ]),
    ];
    const reasons = reasonsFor(card);
    if (reasons) {
      const list = el('div', 'cond-list');
      for (const condition of reasons.conditions) {
        list.appendChild(condNode(condition, true));
      }
      nodes.push(list, el('div', 'meta-line', metaText(reasons)));
    } else {
      nodes.push(el('div', 'meta-line', 'H1 оноо ' + fmtScore(card.h1_score)));
    }
    nodes.push(footNode([next]));
    return {
      nodes: nodes,
      chip: null,
      said: 'Сигнал алга.',
      update: function (tick) {
        setText(sub, candleLine(card.candle_close, tick.now));
        setText(next, nextCheckText(card.next_check, tick.now));
      },
    };
  }

  function buildBlocked(card) {
    const code = String(card.code);
    const title = pick(BLOCK_TITLES, code) || code;
    const proposed = card.side === 'BUY' || card.side === 'SELL';
    const sub = el('div', 'side-sub');
    const next = el('div');

    let strong = proposed
      ? state.timeframe + ' дүрэм ' + card.side + ' санал болгосон ч сигнал илгээгээгүй.'
      : 'Сигнал илгээгээгүй.';
    let detail = '';
    if (code === 'HTF_CONFLICT') {
      detail =
        'H1 оноо ' + fmtScore(card.h1_score) + ' байна. BUY-д H1 оноо +1 ба түүнээс дээш, SELL-д ' +
        MINUS + '1 ба түүнээс доош байх шаардлагатай.';
    } else if (code === 'MARKET_CLOSED') {
      detail = 'Брокерын зах зээл хаалттай цаг байна (ойролцоо цагийн хуваарь).';
    } else if (code === 'INSUFFICIENT_BARS') {
      if (proposed) {
        detail = 'H1 чигийг батлах лаа хүрэлцэхгүй байна.';
      } else {
        strong = 'Дүрмийг тооцоолох лаа хүрэлцэхгүй байна. Сигнал илгээгээгүй.';
        detail = 'Лааны түүх хангалттай болмогц дүрэм ажиллана.';
      }
    } else if (code === 'STALE_FEED') {
      detail = 'Лаа хаагдах үед ханш хуучирсан байсан. Систем хуучин үнээр сигнал гаргадаггүй.';
    } else {
      detail = 'Шүүлтүүр энэ саналыг зогсоосон.';
    }

    const lines = [el('div', 'box-strong', strong), el('div', 'box-text', detail)];
    if (proposed || isNum(card.score)) {
      lines.push(
        el('div', 'meta-line', 'Оноо ' + fmtScore(card.score) + ' / 5 · H1 оноо ' + fmtScore(card.h1_score))
      );
    }
    return {
      nodes: [sideRow('stop', 'warn', title, false, sub), boxNode(lines), footNode([next])],
      chip: chipNode('Сигнал илгээгээгүй', 'soft-warn'),
      said: title + '. Сигнал илгээгээгүй.',
      update: function (tick) {
        setText(sub, candleLine(card.candle_close, tick.now));
        setText(next, nextCheckText(card.next_check, tick.now));
      },
    };
  }

  function buildExpired(card) {
    const sub = el('div', 'side-sub');
    const next = el('div');
    const side = card.side === 'BUY' || card.side === 'SELL' ? card.side + ' сигналын' : 'Сигналын';
    return {
      nodes: [
        sideRow('clock', 'muted', 'Сигналын хугацаа дууссан', false, sub),
        boxNode([
          el('div', 'box-strong', side + ' орох хугацаа дууссан. Хөөж орохгүй.'),
          el('div', 'box-text', 'Үр дүнг сигналын түүхээс харна.'),
        ]),
        footNode([next]),
      ],
      chip: chipNode('Хугацаа дууссан', 'neutral'),
      said: 'Сигналын хугацаа дууссан.',
      update: function (tick) {
        setText(sub, candleLine(card.candle_close, tick.now));
        setText(next, nextCheckText(card.next_check, tick.now));
      },
    };
  }

  function buildFeedDown() {
    const sub = el('div', 'side-sub');
    const last = el('span', 'kv-value');
    const row = el('div', 'kv');
    row.append(el('span', null, 'Сүүлийн ханш'), last);
    const nodes = [
      sideRow('warn', 'down', 'Ханш тасарсан', false, sub),
      boxNode([
        el('div', 'box-strong', 'Ханш дахин ирэх хүртэл сигнал гарахгүй.'),
        el('div', 'box-text', 'Систем хуучин үнээр сигнал гаргадаггүй.'),
      ]),
      row,
    ];
    if (state.snap && state.snap.email === true) {
      const seconds = Number(state.snap.alert_after_seconds);
      const minutes = Number.isFinite(seconds) && seconds > 0 ? Math.max(1, Math.round(seconds / 60)) : 5;
      nodes.push(footNode([minutes + ' минут ханш ирэхгүй бол и-мэйлээр мэдэгдэнэ.']));
    }
    return {
      nodes: nodes,
      chip: chipNode('Зогссон', 'down', 'warn'),
      said: 'Ханш тасарсан. Сигнал гарахгүй.',
      update: function (tick) {
        const card = tick.card || {};
        const age = isNum(card.age) ? card.age : tick.age || 0;
        const minutes = Math.floor(age / 60);
        setText(
          sub,
          'Сүүлийн ханш ' + (minutes >= 1 ? minutes + ' минутын өмнө' : Math.round(age) + ' секундын өмнө') + ' ирсэн'
        );
        setText(
          last,
          isNum(card.last_price) ? fmtPrice(card.last_price) + ' · ' + fmtStamp(card.last_time, tick.now) : DASH
        );
      },
    };
  }

  function buildStarting() {
    return {
      nodes: [
        sideRow('clock', 'muted', 'Ханш ачаалж байна', false, el('div', 'side-sub', 'Лааны түүхийг эх үүсвэрээс татаж байна.')),
        boxNode([
          el('div', 'box-strong', 'Түүх ачаалж дуустал сигнал гарахгүй.'),
          el('div', 'box-text', 'Ихэвчлэн хэдхэн секунд болно.'),
        ]),
      ],
      chip: chipNode('Ачаалж байна', 'neutral'),
      said: 'Ханш ачаалж байна.',
      update: null,
    };
  }

  function buildNoData(failed) {
    return {
      nodes: [
        sideRow(
          failed ? 'warn' : 'clock',
          failed ? 'down' : 'muted',
          failed ? 'Сервертэй холбогдож чадсангүй' : 'Ачаалж байна',
          false,
          el('div', 'side-sub', failed ? 'Хуудас өөрөө дахин оролдож байна.' : 'Серверээс мэдээлэл авч байна.')
        ),
      ],
      chip: null,
      said: null,
      update: null,
    };
  }

  function buildUnknown(card) {
    return {
      nodes: [
        sideRow('pause', 'muted', 'Тодорхойгүй төлөв', false, el('div', 'side-sub', String(card.kind))),
        boxNode([el('div', 'box-text', 'Энэ төлөвийг хуудас танихгүй байна. Сигнал гэж үзэхгүй.')]),
      ],
      chip: null,
      said: null,
      update: null,
    };
  }

  function cardKey(card) {
    const common = [card.kind, state.offset, state.symbol, state.timeframe, sourceName()];
    if (card.kind === 'signal') {
      return common
        .concat([
          card.state, card.side, card.time, card.zone_low, card.zone_high, card.sl, card.tp,
          card.sl_distance, card.tp_distance, card.expires_at, card.valid_seconds, card.grade, card.run,
          experimental() ? 1 : 0,
        ])
        .join('|');
    }
    if (card.kind === 'hold' || card.kind === 'expired' || card.kind === 'blocked') {
      return common
        .concat([
          card.code, card.candle_close, card.next_check, card.score, card.side, card.h1_score,
          reasonsFor(card) ? 1 : 0,
        ])
        .join('|');
    }
    if (card.kind === 'feed_down') {
      return common.concat([state.snap && state.snap.email === true ? 1 : 0]).join('|');
    }
    return common.join('|');
  }

  function buildCard(card) {
    switch (card.kind) {
      case 'signal':
        return buildSignal(card);
      case 'hold':
        return buildHold(card);
      case 'blocked':
        return buildBlocked(card);
      case 'expired':
        return buildExpired(card);
      case 'feed_down':
        return buildFeedDown();
      case 'starting':
        return buildStarting();
      default:
        return buildUnknown(card);
    }
  }

  function renderCard() {
    const section = $('signal');
    const body = $('signal-body');
    const chipHost = $('signal-chip');
    const tick = state.tick;

    let key;
    let card = null;
    if (!tick) {
      key = state.lost ? 'none|failed' : 'none|loading';
    } else {
      card = tick.card && typeof tick.card === 'object' ? tick.card : { kind: 'starting' };
      key = cardKey(card);
    }

    if (key !== view.cardKey) {
      const built = card ? buildCard(card) : buildNoData(state.lost);
      body.replaceChildren.apply(body, built.nodes);
      view.cardKey = key;
      view.cardChip = built.chip;
      view.cardChipShown = null;
      view.cardUpdate = built.update;

      // Told to assistive technology only when the kind, the state or the side changes.
      if (card) {
        const said = [card.kind, card.state, card.side].join('|');
        if (view.cardSaid !== null && view.cardSaid !== said && built.said) {
          announce('Одоогийн сигнал: ' + built.said);
        }
        view.cardSaid = said;
      }
    }

    const stale = state.lost && tick !== null;
    const wanted = stale ? 'lost' : 'own';
    if (wanted !== view.cardChipShown) {
      view.cardChipShown = wanted;
      if (stale) {
        chipHost.replaceChildren(chipNode('Холбоо тасарсан', 'down', 'warn'));
      } else if (view.cardChip) {
        chipHost.replaceChildren(view.cardChip);
      } else {
        chipHost.replaceChildren();
      }
    }
    section.classList.toggle('is-stale', stale);

    if (tick && view.cardUpdate) {
      view.cardUpdate(tick);
    }
    renderCardNotes();
  }

  function noteNode(kind, iconName, text) {
    const note = el('div', 'note note-' + kind);
    note.append(icon(iconName, 14, 2.2), el('span', null, text));
    return note;
  }

  function renderCardNotes() {
    const host = $('signal-notes');
    const tick = state.tick;
    const stale = state.lost && tick !== null;
    const closed = tick !== null && tick.market_closed === true;
    const since = stale ? fmtStamp(tick.now, estimatedNow()) : '';
    // Ticks arrive but /api/state keeps failing: the card and the price are current, the rest is not.
    const behind = !stale && tick !== null && snapshotJob.fails >= SNAPSHOT_FAILS_SHOWN;
    const key = [stale ? 1 : 0, closed ? 1 : 0, since, behind ? 1 : 0].join('|');
    if (key === view.notesKey) {
      return;
    }
    view.notesKey = key;
    const nodes = [];
    if (behind) {
      const words =
        'Серверээс бүрэн мэдээлэл авч чадахгүй байна. Сигналын түүх, шалтгаан, мэдэгдлийн жагсаалт ' +
        'шинэчлэгдээгүй байж магадгүй. Хуудас өөрөө дахин оролдож байна.';
      nodes.push(noteNode('down', 'warn', words));
      announce(words);
    }
    if (stale) {
      nodes.push(
        noteNode(
          'down',
          'warn',
          'Сервертэй холбоо тасарсан. Доорх мэдээлэл одоогийнх биш (сүүлд шинэчлэгдсэн: ' + since + ').'
        )
      );
    }
    if (closed) {
      nodes.push(
        noteNode(
          'warn',
          'clock',
          'Брокерын зах зээл хаалттай байж магадгүй (ойролцоо цагийн хуваарь). Энэ үед сигнал гарахгүй.'
        )
      );
    }
    host.replaceChildren.apply(host, nodes);
    host.hidden = nodes.length === 0;
  }

  // ----------------------------------------------------------- snapshot parts

  function renderSnapshotParts() {
    safe(renderSource);
    safe(syncSupported);
    safe(syncForecast);
    safe(renderToday);
    safe(renderRules);
    safe(renderReasons);
    safe(renderLog);
    safe(renderAlerts);
    safe(renderEmail);
    safe(updateMarkers);
  }

  function renderSource() {
    const full = sourceFull();
    const name = state.source.name ? sourceName() : DASH;
    setText($('price-source'), full);
    for (const node of document.querySelectorAll('[data-source]')) {
      setText(node, node.getAttribute('data-source') === 'full' ? full : name);
    }
    updateTitle();
  }

  function renderToday() {
    const today = state.snap && state.snap.today;
    if (!today || typeof today !== 'object') {
      setText($('today-total'), DASH);
      setText($('today-split'), DASH);
      return;
    }
    setText($('today-total'), isNum(today.total) ? String(today.total) : DASH);
    setText(
      $('today-split'),
      (isNum(today.buy) ? today.buy : DASH) + ' BUY · ' + (isNum(today.sell) ? today.sell : DASH) + ' SELL'
    );
  }

  function renderRules() {
    const rules = state.snap && state.snap.rules;
    if (!rules || typeof rules !== 'object') {
      setText($('rules-status'), DASH);
      setText($('rules-note'), DASH);
      return;
    }
    if (rules.status === 'experimental') {
      setText($('rules-status'), 'Туршилтын');
      setText($('rules-note'), 'Backtest хийгдээгүй');
    } else {
      setText($('rules-status'), String(rules.status === undefined ? DASH : rules.status));
      setText($('rules-note'), '');
    }
  }

  function renderReasons() {
    const host = $('reasons-body');
    const snap = state.snap;
    const reasons = snap && snap.reasons;
    const now = snap && isNum(snap.now) ? snap.now : null;
    const key = JSON.stringify(reasons || null) + '|' + state.offset + '|' + (now === null ? '' : dayIndex(now));
    if (key === view.reasonsKey) {
      return;
    }
    view.reasonsKey = key;

    if (!reasons || typeof reasons !== 'object') {
      host.replaceChildren(el('span', 'meta-line', 'Дүрэм хараахан тооцоологдоогүй. Лаа хаагдахад энд гарна.'));
      return;
    }
    const nodes = [];
    const score = el('div', 'reason-score');
    score.append('Оноо ', el('span', 'tone-' + (toneOf(reasons.score) === 'zero' ? 'muted' : toneOf(reasons.score)), fmtScore(reasons.score)), ' / 5');
    nodes.push(score);
    for (const condition of Array.isArray(reasons.conditions) ? reasons.conditions : []) {
      if (condition && typeof condition === 'object') {
        nodes.push(condNode(condition, false));
      }
    }
    if (reasons.blocked) {
      const code = String(reasons.blocked);
      nodes.push(el('span', 'tag tag-warn', 'Шүүлтүүр: ' + (pick(BLOCK_TITLES, code) || code)));
    }
    const candle = isNum(reasons.candle_close) ? ' · ' + fmtStamp(reasons.candle_close, now) + '-д хаагдсан лаа' : '';
    nodes.push(el('div', 'meta-line', metaText(reasons) + candle));
    host.replaceChildren.apply(host, nodes);
  }

  function sideWord(side) {
    const buy = side === 'BUY';
    const sell = side === 'SELL';
    const node = el('span', 'side-word' + (buy ? ' tone-up' : sell ? ' tone-down' : ''));
    if (buy || sell) {
      node.appendChild(icon(buy ? 'up' : 'down', 12, 3));
    }
    node.appendChild(el('span', null, String(side === undefined || side === null ? DASH : side)));
    return node;
  }

  function outcomeNode(row, now) {
    const outcome = row.outcome && typeof row.outcome === 'object' ? row.outcome : {};
    if (outcome.kind === 'valid') {
      return el('span', 'tag tag-up', 'Хүчинтэй · ' + fmtStamp(row.expires_at, now) + ' хүртэл');
    }
    if (outcome.kind === 'tp') {
      return el('span', 'tag tag-up', 'TP-д хүрсэн · ' + fmtStamp(outcome.time, now));
    }
    if (outcome.kind === 'sl') {
      return el('span', 'tag tag-down', 'SL-д хүрсэн · ' + fmtStamp(outcome.time, now));
    }
    if (outcome.kind === 'none') {
      return el('span', 'tag tag-neutral', 'Үр дүн гараагүй');
    }
    return el('span', 'tag tag-neutral', DASH);
  }

  function cell(className, child) {
    const td = el('td', className);
    if (typeof child === 'string') {
      td.textContent = child;
    } else if (child) {
      td.appendChild(child);
    }
    return td;
  }

  function renderLog() {
    const snap = state.snap;
    if (!snap) {
      return;
    }
    const rows = Array.isArray(snap.log) ? snap.log.filter(function (row) { return row && typeof row === 'object'; }) : [];
    const now = isNum(snap.now) ? snap.now : null;
    const newsOff = snap.rules && Array.isArray(snap.rules.filters_off) && snap.rules.filters_off.indexOf('NEWS_WINDOW') !== -1;
    const key = JSON.stringify(rows) + '|' + state.offset + '|' + (now === null ? '' : dayIndex(now)) + '|' + (newsOff ? 1 : 0);
    if (key === view.logKey) {
      return;
    }
    view.logKey = key;

    const body = $('log-body');
    const list = rows.map(function (row) {
      const tr = el('tr');
      tr.append(
        cell('time', fmtStamp(row.time, now)),
        cell('', sideWord(row.side)),
        cell('', outcomeNode(row, now)),
        cell('num', fmtPrice(row.entry)),
        cell('num', fmtPrice(row.sl)),
        cell('num', fmtPrice(row.tp)),
        cell('', String(row.grade === undefined || row.grade === null ? DASH : row.grade))
      );
      return tr;
    });
    body.replaceChildren.apply(body, list);
    show($('log-wrap'), rows.length > 0);
    const empty = $('log-empty');
    setText(empty, 'Одоогоор сигнал алга. Шинэ сигнал гарахад энд нэмэгдэнэ.');
    show(empty, rows.length === 0);

    const parts = [];
    if (rows.length > 0) {
      parts.push('Сүүлийн ' + rows.length + ' сигнал');
    }
    if (newsOff) {
      parts.push('мэдээний шүүлтүүргүй');
    }
    setText($('log-count'), parts.join(' · '));
  }

  function renderAlerts() {
    const snap = state.snap;
    if (!snap) {
      return;
    }
    const alerts = Array.isArray(snap.alerts) ? snap.alerts.filter(function (a) { return a && typeof a === 'object'; }) : [];
    const now = isNum(snap.now) ? snap.now : null;
    const key = JSON.stringify(alerts) + '|' + state.offset + '|' + (now === null ? '' : dayIndex(now));
    if (key === view.alertsKey) {
      return;
    }
    view.alertsKey = key;

    const list = $('alerts-list');
    const items = alerts.map(function (alert) {
      const level = String(alert.level);
      const item = el('li', 'alert');
      const shape = el('span', 'shape' + (pick(LEVEL_WORDS, level) ? ' shape-' + level : ''));
      shape.setAttribute('aria-hidden', 'true');
      const body = el('div', 'alert-body');
      body.append(
        el('div', 'alert-text', String(alert.text === undefined || alert.text === null ? '' : alert.text)),
        el('div', 'alert-meta', fmtStamp(alert.time, now) + ' · ' + (pick(LEVEL_WORDS, level) || level))
      );
      item.append(shape, body);
      return item;
    });
    list.replaceChildren.apply(list, items);
    show(list, alerts.length > 0);
    const empty = $('alerts-empty');
    setText(empty, 'Мэдэгдэл алга.');
    show(empty, alerts.length === 0);
  }

  function renderEmail() {
    const snap = state.snap;
    if (!snap) {
      return;
    }
    const on = snap.email === true;
    setText(
      $('email-note'),
      on
        ? 'Шинэ сигнал бүр, мөн ханш удаан тасарсан үед и-мэйл давхар очно.'
        : 'И-мэйл тохируулаагүй. Мэдэгдэл зөвхөн энэ хуудсанд гарна.'
    );
    show($('email-test'), on);
    if (!on) {
      setResult('', '');
    }
  }

  function setResult(text, kind) {
    const node = $('email-result');
    node.className = kind ? 'result is-' + kind : 'result';
    setText(node, text);
  }

  // -------------------------------------------------------------------- chart

  /*
   * Times go to the chart shifted into the display zone (UTC seconds plus the
   * offset), so the library's own day and hour boundaries fall on that zone's
   * midnight. The two formatters below therefore read the time they are given
   * with UTC getters and add nothing. Everything kept on this side (chart.bars,
   * chart.last) stays in real UTC seconds.
   */
  function toChartTime(seconds) {
    return seconds + chart.shift;
  }

  function chartBar(bar) {
    const out = { time: toChartTime(bar.time), open: bar.open, high: bar.high, low: bar.low, close: bar.close };
    if (bar.closed === false) {
      // Still forming: drawn hollow, so that it is not read as a finished candle.
      const rising = bar.close >= bar.open;
      out.color = rising ? COLOR.upSoft : COLOR.downSoft;
      out.borderColor = rising ? COLOR.up : COLOR.down;
      out.wickColor = out.borderColor;
    }
    return out;
  }

  /* A bar as the page keeps it. Only the server's own `closed: false` makes a candle a forming one. */
  function barOf(row) {
    return { time: row.time, open: row.open, high: row.high, low: row.low, close: row.close, closed: row.closed !== false };
  }

  function validBar(bar) {
    return (
      bar && typeof bar === 'object' && isNum(bar.time) && isNum(bar.open) && isNum(bar.high) && isNum(bar.low) && isNum(bar.close)
    );
  }

  function tickMark(time, type) {
    if (typeof time !== 'number') {
      return null;
    }
    const d = new Date(time * 1000);
    if (type === 0) {
      return String(d.getUTCFullYear());
    }
    if (type === 1) {
      return d.getUTCMonth() + 1 + '-р сар';
    }
    if (type === 2) {
      return pad2(d.getUTCMonth() + 1) + '.' + pad2(d.getUTCDate());
    }
    return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  function crosshairTime(time) {
    if (typeof time !== 'number') {
      return '';
    }
    const d = new Date(time * 1000);
    const day = d.getUTCFullYear() + '.' + pad2(d.getUTCMonth() + 1) + '.' + pad2(d.getUTCDate());
    if (pick(MONTHS, chart.tf) !== undefined) {
      return day.slice(0, 7); // a month candle: the month is the whole answer
    }
    if (chart.tf === 'D1' || chart.tf === 'W1') {
      return day;
    }
    return day + ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  function chartMessage(text) {
    const node = $('chart-msg');
    setText(node, text);
    show(node, text !== '');
  }

  function initChart() {
    const LWC = window.LightweightCharts;
    const host = $('chart-el');
    if (!LWC || !host) {
      chartMessage('График ачаалагдсангүй. Хуудсыг дахин ачаална уу.');
      return;
    }
    try {
      chart.api = LWC.createChart(host, {
        autoSize: true, // follows the size of its container
        layout: {
          background: { type: LWC.ColorType.Solid, color: COLOR.card },
          textColor: COLOR.dim,
          fontFamily: MONO,
          fontSize: 11,
          attributionLogo: true, // the licence asks for the attribution
        },
        grid: { vertLines: { color: COLOR.grid }, horzLines: { color: COLOR.grid } },
        rightPriceScale: { borderColor: COLOR.border, scaleMargins: { top: 0.12, bottom: 0.12 } },
        timeScale: {
          borderColor: COLOR.border,
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 4,
          tickMarkFormatter: tickMark,
        },
        crosshair: {
          mode: LWC.CrosshairMode.Normal,
          vertLine: { color: COLOR.dim, labelBackgroundColor: COLOR.raised },
          horzLine: { color: COLOR.dim, labelBackgroundColor: COLOR.raised },
        },
        localization: { timeFormatter: crosshairTime, priceFormatter: fmtPrice },
        // A vertical swipe over the chart scrolls the page; a horizontal one moves the chart.
        handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      });
      // The mouse wheel scrolls the page as everywhere else. With Ctrl held (or a pinch on a
      // touchpad) it zooms the chart; a sideways wheel moves the chart.
      host.addEventListener(
        'wheel',
        function (event) {
          if (!event.ctrlKey && !event.metaKey && Math.abs(event.deltaY) >= Math.abs(event.deltaX)) {
            event.stopPropagation();
          }
        },
        { capture: true, passive: true }
      );
      chart.series = chart.api.addSeries(LWC.CandlestickSeries, {
        upColor: COLOR.up,
        downColor: COLOR.down,
        wickUpColor: COLOR.up,
        wickDownColor: COLOR.down,
        borderVisible: true, // closed candles: border and body alike; the forming one is hollow
        borderUpColor: COLOR.up,
        borderDownColor: COLOR.down,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        priceLineVisible: true,
        priceLineColor: COLOR.accent,
        priceLineStyle: LWC.LineStyle.SparseDotted, // thinner and sparser than the entry zone's lines
        priceLineWidth: 1,
        lastValueVisible: true,
        // Keep SL and TP of the current signal inside the visible price range.
        autoscaleInfoProvider: function (original) {
          const info = original();
          const levels = chart.levels;
          if (info !== null && info.priceRange && levels) {
            info.priceRange.minValue = Math.min(info.priceRange.minValue, levels.min);
            info.priceRange.maxValue = Math.max(info.priceRange.maxValue, levels.max);
          }
          return info;
        },
      });
      chart.markers = LWC.createSeriesMarkers(chart.series, []);
      chart.api.subscribeCrosshairMove(onCrosshair);
      safe(function () {
        initForecastSeries(LWC);
      });
    } catch (error) {
      chart.api = null;
      chart.series = null;
      chart.markers = null;
      chartMessage('График ачаалагдсангүй. Хуудсыг дахин ачаална уу.');
      if (window.console && console.error) {
        console.error(error);
      }
    }
  }

  /* Fetch the picked timeframe's candles and replace the series. The newest request wins. */
  async function loadCandles(resetView) {
    if (!chart.series || state.leaving) {
      return;
    }
    const tf = chart.tf;
    const token = ++chart.request;
    chart.loading = true;
    chart.startedAt = Date.now();
    // chart.stale stays as it is until this load has succeeded (setBars). Cleared here, a load
    // that is slow or fails would pass for a chart in order until its answer came.
    try {
      const body = await getJSON('/api/candles?timeframe=' + encodeURIComponent(tf) + '&limit=' + CANDLE_LIMIT);
      if (token !== chart.request) {
        return; // a newer choice was made meanwhile: only its answer goes into the chart
      }
      const bars = [];
      let before = -Infinity;
      for (const row of body && Array.isArray(body.bars) ? body.bars : []) {
        if (validBar(row) && row.time > before) {
          bars.push(barOf(row));
          before = row.time;
        }
      }
      chart.info = {
        history: body && body.history === 'short' ? 'short' : 'full',
        partialFirst: Boolean(body && body.partial_first === true),
        gaps: body && isNum(body.gaps) ? body.gaps : 0,
      };
      chart.tailFails = 0;
      setBars(tf, bars, resetView);
      safe(renderChartNote);
    } catch (error) {
      if (token === chart.request) {
        if (error && error.message === 'HTTP 422' && tf !== state.timeframe) {
          // The server cannot show this timeframe (another source than when it was picked):
          // back to the signal timeframe, which every source has.
          chart.loading = false;
          chart.supported = (chart.supported || TF_ORDER).filter(function (id) {
            return id !== tf;
          });
          pickTimeframe(state.timeframe);
          return;
        }
        // Not answered after all. Without this the ticks that follow would move the last
        // known time on, and the candle that missed its close would stay wrong for good.
        chart.stale = true;
        if (!(error instanceof AuthError) && chart.bars.length === 0) {
          chartMessage('Лааны түүхийг татаж чадсангүй. Дахин оролдож байна.');
        }
      }
    } finally {
      if (token === chart.request) {
        chart.loading = false;
      }
    }
  }

  /* The guarded form: used by the gap check, a stream reconnect and a page that became visible. */
  function requestCandles() {
    if (!chart.series || chart.loading || Date.now() - chart.startedAt < CANDLE_PAUSE_MS) {
      return;
    }
    loadCandles(chart.loadedTf !== chart.tf); // nothing on screen yet: start at the newest candle
  }

  function setBars(tf, bars, resetView) {
    chart.stale = false; // a fresh load answers every gap seen so far
    chart.shift = state.offset * 3600;
    chart.bars = bars;
    chart.index = new Map();
    for (let i = 0; i < bars.length; i += 1) {
      chart.index.set(bars[i].time, i);
    }
    chart.loadedTf = tf;
    chart.last[tf] = bars.length ? bars[bars.length - 1].time : null;
    chart.hover = null;
    chart.series.setData(bars.map(chartBar));
    safe(syncForecastSlots); // before the view is reset: the time axis is complete then
    if (resetView && bars.length) {
      chart.api.timeScale().resetTimeScale(); // the newest candles at the default zoom
    }
    chartMessage(
      bars.length
        ? ''
        : tf === state.timeframe
          ? 'Лаа хараахан алга. Ханш ирэхэд график гарна.'
          : tf + ' хугацааны бүтэн лаа хараахан алга: түүх хүрэлцэхгүй байна.'
    );
    updateMarkers();
    renderLegend();
    safe(syncForecast);
  }

  function rememberBar(bar) {
    const copy = barOf(bar);
    const at = chart.index.get(bar.time);
    if (at === undefined) {
      chart.index.set(bar.time, chart.bars.length);
      chart.bars.push(copy);
    } else {
      chart.bars[at] = copy;
    }
  }

  /*
   * The newest bars of a tick. The tick carries the signal timeframe and H1; for any
   * other timeframe on screen its newest candles are asked for (requestTail).
   */
  function applyBars(tickBars) {
    if (!chart.series || !tickBars || typeof tickBars !== 'object') {
      return;
    }
    const tf = chart.tf;
    if (Array.isArray(tickBars[tf])) {
      applyList(tf, tickBars[tf]);
    } else if (Object.keys(tickBars).length > 0) {
      // ({} while the feed is down: nothing moves then.)
      if (chart.loadedTf !== tf) {
        requestCandles(); // still on their way, or an earlier load failed: then ask again
      } else {
        requestTail();
      }
    }
  }

  /* The newest candles of a timeframe the tick does not carry. One request at a time, never a queue. */
  async function requestTail() {
    const tf = chart.tf;
    if (chart.tailBusy || chart.loading || state.leaving || chart.loadedTf !== tf) {
      return;
    }
    if (chart.stale) {
      requestCandles(); // a candle on screen missed its close: the whole history, not a tail
      return;
    }
    if (Date.now() - chart.tailAt < TAIL_EVERY_MS) {
      return;
    }
    chart.tailBusy = true;
    chart.tailAt = Date.now();
    const token = chart.request; // a new choice or a reload makes this answer an old one
    try {
      const body = await getJSON('/api/candles?timeframe=' + encodeURIComponent(tf) + '&limit=' + TAIL_BARS);
      if (token === chart.request && !chart.loading && chart.loadedTf === tf) {
        chart.tailFails = 0;
        applyList(tf, body && Array.isArray(body.bars) ? body.bars : []);
      }
    } catch (error) {
      if (token === chart.request) {
        chart.tailFails += 1;
      }
    } finally {
      chart.tailBusy = false;
      safe(renderChartNote);
    }
  }

  /* Bars of a tick or of a tail request. Only bars at or after the last known time are passed on. */
  function applyList(tf, list) {
    if (!chart.series || tf !== chart.tf || !Array.isArray(list) || list.length === 0) {
      return;
    }
    if (chart.loadedTf !== tf) {
      requestCandles(); // still on their way, or an earlier load failed: then ask again
      return;
    }
    let last = chart.last[tf];
    if (last === null || last === undefined) {
      requestCandles();
      return;
    }
    // When the list no longer carries the candle we hold, that candle never got its close: load again.
    let reload = validBar(list[0]) && list[0].time > last;
    for (const row of list) {
      if (!validBar(row) || row.time < last) {
        continue;
      }
      if (row.time > nextOpen(last, tf)) {
        reload = true; // a gap: more than one candle ahead
        break;
      }
      const bar = barOf(row);
      try {
        chart.series.update(chartBar(bar));
      } catch (error) {
        reload = true;
        break;
      }
      rememberBar(bar);
      last = bar.time;
      chart.last[tf] = last;
    }
    if (reload) {
      chart.stale = true;
    }
    if (chart.stale) {
      requestCandles(); // asked again on every tick until a load succeeds
    }
    if (chart.hover === null) {
      renderLegend();
    }
  }

  function updateMarkers() {
    if (!chart.markers) {
      return;
    }
    const tf = chart.loadedTf;
    const log = state.snap && Array.isArray(state.snap.log) ? state.snap.log : [];
    const out = [];
    if (tf && chart.bars.length) {
      const first = chart.bars[0].time;
      const last = chart.bars[chart.bars.length - 1].time;
      const byKey = new Map();
      let newest = true; // the log is newest first
      for (const row of log) {
        if (!row || !isNum(row.time) || (row.side !== 'BUY' && row.side !== 'SELL')) {
          continue;
        }
        // The arrow's direction and place tell the side. The word is written on the newest
        // signal and on the first one of a run, so back-to-back signals do not pile their
        // labels on top of each other.
        const worded = newest || row.run === 1;
        newest = false;
        // The marker sits on the candle the signal falls in, whatever the chart's timeframe.
        const at = candleOpen(row.time, tf);
        if (at < first || at > last || !chart.index.has(at)) {
          continue;
        }
        const key = at + row.side;
        const known = byKey.get(key);
        if (known) {
          known.worded = known.worded || worded;
        } else {
          byKey.set(key, { at: at, side: row.side, worded: worded });
        }
      }
      for (const item of byKey.values()) {
        const buy = item.side === 'BUY';
        out.push({
          time: toChartTime(item.at),
          position: buy ? 'belowBar' : 'aboveBar',
          shape: buy ? 'arrowUp' : 'arrowDown',
          color: buy ? COLOR.up : COLOR.down,
          text: item.worded ? item.side : '',
        });
      }
      out.sort(function (a, b) {
        return a.time - b.time;
      });
    }
    chart.markers.setMarkers(out);
  }

  /* TP and SL (dashed) and the entry zone (dotted) while the card is a signal; nothing otherwise. */
  function syncPriceLines() {
    if (!chart.series) {
      return;
    }
    const card = state.tick && state.tick.card;
    const on =
      card && card.kind === 'signal' && isNum(card.tp) && isNum(card.sl) && isNum(card.zone_low) && isNum(card.zone_high);
    const key = on ? [card.time, card.side, card.tp, card.sl, card.zone_low, card.zone_high].join('|') : '';
    if (key === chart.linesKey) {
      return;
    }
    for (const line of chart.lines) {
      try {
        chart.series.removePriceLine(line);
      } catch (error) {
        /* already gone */
      }
    }
    chart.lines = [];
    chart.linesKey = key;
    chart.levels = null;
    if (!on) {
      return;
    }
    const style = window.LightweightCharts.LineStyle;
    const add = function (price, color, lineStyle, lineWidth, labelled, title) {
      chart.lines.push(
        chart.series.createPriceLine({
          price: price,
          color: color,
          lineStyle: lineStyle,
          lineWidth: lineWidth,
          axisLabelVisible: labelled,
          title: title,
        })
      );
    };
    add(card.tp, COLOR.up, style.Dashed, 2, true, 'TP');
    add(card.sl, COLOR.down, style.Dashed, 2, true, 'SL');
    add(card.zone_high, COLOR.accent, style.Dotted, 2, false, '');
    add(card.zone_low, COLOR.accent, style.Dotted, 2, false, '');
    chart.levels = { min: Math.min(card.tp, card.sl), max: Math.max(card.tp, card.sl) };
  }

  function onCrosshair(param) {
    let bar = null;
    if (param && param.time !== undefined && param.point && param.seriesData && chart.series) {
      const data = param.seriesData.get(chart.series);
      if (data && isNum(data.time) && isNum(data.open) && isNum(data.close)) {
        const time = data.time - chart.shift;
        const kept = chart.bars[chart.index.get(time)];
        bar = {
          time: time,
          open: data.open,
          high: data.high,
          low: data.low,
          close: data.close,
          closed: !kept || kept.closed !== false,
        };
      }
    }
    chart.hover = bar;
    renderLegend();
  }

  /* O H L C and the change from the previous close: the bar under the crosshair, otherwise the last bar. */
  function renderLegend() {
    const bars = chart.bars;
    let bar = chart.hover;
    let previous = null;
    if (bar) {
      const at = chart.index.get(bar.time);
      if (at !== undefined && at > 0) {
        previous = bars[at - 1];
      }
    } else if (bars.length) {
      bar = bars[bars.length - 1];
      previous = bars.length > 1 ? bars[bars.length - 2] : null;
    }
    const change = $('ohlc-chg');
    show($('ohlc-forming'), Boolean(bar) && bar.closed === false);
    if (!bar) {
      setText($('ohlc-o'), 'O ' + DASH);
      setText($('ohlc-h'), 'H ' + DASH);
      setText($('ohlc-l'), 'L ' + DASH);
      setText($('ohlc-c'), 'C ' + DASH);
      setText(change, '');
      return;
    }
    setText($('ohlc-o'), 'O ' + fmtPrice(bar.open));
    setText($('ohlc-h'), 'H ' + fmtPrice(bar.high));
    setText($('ohlc-l'), 'L ' + fmtPrice(bar.low));
    setText($('ohlc-c'), 'C ' + fmtPrice(bar.close));
    const delta = bar.close - (previous ? previous.close : bar.open);
    setText(change, fmtSigned(delta));
    const className = 'ohlc-change ' + (delta < 0 ? 'tone-down' : 'tone-up');
    if (change.className !== className) {
      change.className = className;
    }
  }

  function tfSupported(tf) {
    return knownTf(tf) && (chart.supported === null || chart.supported.indexOf(tf) !== -1);
  }

  /* The chart's title names what is drawn: source, its symbol, timeframe. */
  function updateTitle() {
    const subject = state.source.name ? sourceFull() : state.symbol;
    setText($('chart-title'), subject + ' · ' + chart.tf);
    const host = $('chart-el');
    if (host) {
      host.setAttribute('aria-label', subject + ' ' + chart.tf + ' лааны график');
    }
  }

  /* Day, week and month candles carry a date on the time axis, shorter ones a time of day. */
  function applyTimeScale() {
    if (chart.api) {
      const long = chart.tf === 'D1' || chart.tf === 'W1' || pick(MONTHS, chart.tf) !== undefined;
      chart.api.applyOptions({ timeScale: { timeVisible: !long } });
    }
  }

  function buildTfMenu() {
    const menu = $('tf-menu');
    const parts = [];
    for (const group of TF_GROUPS) {
      const section = el('div', 'tf-group');
      section.setAttribute('role', 'group');
      section.setAttribute('aria-label', group.title);
      section.appendChild(el('div', 'tf-group-title', group.title));
      for (const item of group.items) {
        const row = el('div', 'tf-row');
        row.setAttribute('data-row', item[0]);
        const choose = el('button', 'tf-choose');
        choose.type = 'button';
        choose.setAttribute('data-pick', item[0]);
        choose.append(el('span', 'tf-words', item[1]), el('span', 'tf-code', item[0]));
        const star = el('button', 'tf-star');
        star.type = 'button';
        star.setAttribute('data-star', item[0]);
        star.appendChild(icon('star', 17, 1.8));
        row.append(choose, star);
        section.appendChild(row);
      }
      parts.push(section);
    }
    parts.push(
      el(
        'p',
        'tf-foot',
        'Энэ нь зөвхөн графикийн харагдац. Сигнал M15 лаан дээр, шүүлтүүр нь H1 дээр тооцогдсон хэвээр.'
      )
    );
    menu.replaceChildren.apply(menu, parts);
  }

  /* The quick buttons (starred timeframes), the menu button and the menu rows, from chart.tf and chart.favorites. */
  function renderTimeframes() {
    setText($('tf-current'), chart.tf);
    $('tf-button').title = 'Графикийн хугацаа: ' + (pick(TF_NAMES, chart.tf) || chart.tf);
    const quick = $('tf-quick');
    const wanted = chart.favorites.filter(tfSupported);
    const key = wanted.join(',');
    if (quick.getAttribute('data-key') !== key) {
      quick.setAttribute('data-key', key);
      quick.replaceChildren.apply(
        quick,
        wanted.map(function (tf) {
          const button = el('button', null, tf);
          button.type = 'button';
          button.setAttribute('data-tf', tf);
          button.title = pick(TF_NAMES, tf) || tf;
          return button;
        })
      );
      quick.hidden = wanted.length === 0;
    }
    for (const button of quick.querySelectorAll('[data-tf]')) {
      button.setAttribute('aria-pressed', button.getAttribute('data-tf') === chart.tf ? 'true' : 'false');
    }
    for (const row of $('tf-menu').querySelectorAll('[data-row]')) {
      const tf = row.getAttribute('data-row');
      const choose = row.querySelector('[data-pick]');
      const star = row.querySelector('[data-star]');
      const starred = chart.favorites.indexOf(tf) !== -1;
      const able = tfSupported(tf);
      const name = pick(TF_NAMES, tf) || tf;
      row.classList.toggle('is-current', tf === chart.tf);
      if (tf === chart.tf) {
        choose.setAttribute('aria-current', 'true');
      } else {
        choose.removeAttribute('aria-current');
      }
      choose.disabled = !able;
      choose.title = able ? '' : 'Энэ эх үүсвэр уг хугацааг өгдөггүй';
      star.setAttribute('aria-pressed', starred ? 'true' : 'false');
      star.setAttribute('aria-label', name + (starred ? ': дуртайгаас хасах' : ': дуртайд нэмэх'));
      star.title = starred ? 'Дуртайгаас хасах' : 'Дуртайд нэмэх';
    }
  }

  /* A star only changes the list of quick buttons. The chart and the picked timeframe stay as they are. */
  function toggleFavorite(tf) {
    if (!knownTf(tf)) {
      return;
    }
    const at = chart.favorites.indexOf(tf);
    if (at === -1) {
      chart.favorites.push(tf);
    } else {
      chart.favorites.splice(at, 1);
    }
    const kept = chart.favorites;
    chart.favorites = TF_ORDER.filter(function (id) {
      return kept.indexOf(id) !== -1;
    });
    writeStore(STORE.favorites, JSON.stringify(chart.favorites));
    renderTimeframes();
  }

  function setTfMenu(open) {
    const menu = $('tf-menu');
    chart.menuOpen = open;
    menu.hidden = !open;
    $('tf-button').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) {
      return;
    }
    // Keep the menu inside the window: open it towards the side with more room, then move
    // it sideways and limit its height where it would still stick out.
    menu.style.transform = '';
    menu.style.maxHeight = '';
    const anchor = $('tf-button').getBoundingClientRect();
    menu.classList.toggle('opens-right', anchor.left + anchor.width / 2 < document.documentElement.clientWidth / 2);
    const box = menu.getBoundingClientRect();
    const width = document.documentElement.clientWidth;
    let shift = 0;
    if (box.right > width - 8) {
      shift = width - 8 - box.right;
    }
    if (box.left + shift < 8) {
      shift = 8 - box.left;
    }
    if (shift) {
      menu.style.transform = 'translateX(' + Math.round(shift) + 'px)';
    }
    const room = window.innerHeight - box.top - 8;
    if (box.height > room) {
      menu.style.maxHeight = Math.max(180, Math.floor(room)) + 'px';
    }
    const current = menu.querySelector('.is-current [data-pick]');
    if (current) {
      current.focus();
    }
  }

  function wireTimeframes() {
    $('tf-quick').addEventListener('click', function (event) {
      const button = event.target.closest('[data-tf]');
      if (button) {
        pickTimeframe(button.getAttribute('data-tf'));
      }
    });
    $('tf-button').addEventListener('click', function () {
      setTfMenu(!chart.menuOpen);
    });
    $('tf-menu').addEventListener('click', function (event) {
      const star = event.target.closest('[data-star]');
      if (star) {
        toggleFavorite(star.getAttribute('data-star'));
        return;
      }
      const choose = event.target.closest('[data-pick]');
      if (choose && !choose.disabled) {
        pickTimeframe(choose.getAttribute('data-pick'));
        setTfMenu(false);
        $('tf-button').focus();
      }
    });
    document.addEventListener('click', function (event) {
      if (chart.menuOpen && !event.target.closest('.tf-pick')) {
        setTfMenu(false);
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && chart.menuOpen) {
        setTfMenu(false);
        $('tf-button').focus();
      }
    });
  }

  function initTimeframes() {
    buildTfMenu();
    renderTimeframes();
    updateTitle();
    applyTimeScale();
  }

  /* What the server says it can show. A timeframe picked earlier that this source lacks falls back. */
  function syncSupported() {
    const list = state.snap && state.snap.chart && state.snap.chart.timeframes;
    if (!Array.isArray(list)) {
      return;
    }
    const next = list.filter(knownTf);
    if (chart.supported !== null && chart.supported.join(',') === next.join(',')) {
      return;
    }
    chart.supported = next;
    renderTimeframes();
    if (!tfSupported(chart.tf) && next.length) {
      pickTimeframe(tfSupported(state.timeframe) ? state.timeframe : next[0]);
    }
  }

  // ----------------------------------------------------------------- forecast

  /*
   * The forecast (snapshot.prediction, docs/WEB.md): the expected close of the next four M15
   * candles, drawn as a pink dashed line with a faint band. It is shown beside the signals
   * and is never part of one: nothing here touches the signal card, the notification, the
   * sound or the tab title.
   *
   * Rules kept here:
   * - The page draws only what the server sent. It never makes, moves or stretches a line.
   * - Only on the M15 chart. On another timeframe nothing is converted and nothing is drawn.
   * - Only while the forecast fits the candles on screen: it must have been made on the close
   *   of the newest closed candle. When a newer candle closes the old line goes at once, and
   *   the new one comes with the next snapshot.
   * - Never without its measured record. When that record is no better than "the price
   *   stays where it is", the line is dimmer and the words say so.
   */

  const FORECAST_WHY = {
    loading: 'ачаалж байна',
    chart: 'график ачаалж байна',
    lost: 'сервертэй холбоо тасарсан',
    feed_down: 'ханш тасарсан',
    no_model: 'модель хараахан сургагдаагүй',
    not_enough_candles: 'лаа хүрэлцэхгүй байна',
    failed: 'тооцоолж чадсангүй',
    waiting: 'шинэ лааны таамгийг хүлээж байна',
  };
  const FORECAST_MORE = {
    no_model: ' Сургах тушаал: python -m backend.scripts.train_forecast. Дараа нь вэб аппыг дахин асаана.',
    not_enough_candles: ' Таамаг гаргахад урт, тасралтгүй M15 түүх хэрэгтэй.',
    feed_down: ' Ханш сэргэхэд таамаг дахин гарна.',
  };
  const DIRECTION_WORDS = { up: 'өсөх', down: 'буурах', flat: 'тодорхойгүй' };

  /*
   * The forecast's series. None of them carries a price label, a price line or a crosshair
   * dot, and none takes part in the price scale: the candles alone decide what range is in
   * view, so a forecast can never squeeze them.
   */
  function initForecastSeries(LWC) {
    const base = {
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      priceLineVisible: false, // no line across the chart that could be read as a level
      lastValueVisible: false, // no label on the price axis that could be read as the live price
      crosshairMarkerVisible: false,
      pointMarkersVisible: false,
      autoscaleInfoProvider: function () {
        return null;
      },
    };
    const band = Object.assign({}, base, { color: FORECAST_COLOR.band, lineWidth: 1, lineStyle: LWC.LineStyle.Solid });
    // The four expected closes are dots joined by the dashed line: at the default zoom the
    // line is only four candles long, and dashes alone are easy to miss.
    const line = Object.assign({}, base, {
      color: FORECAST_COLOR.line,
      lineWidth: 2,
      lineStyle: LWC.LineStyle.Dashed,
      pointMarkersVisible: true,
      pointMarkersRadius: 2.5,
    });
    chart.forecast = {
      // Never drawn. It only keeps the time axis four candles ahead (syncForecastSlots).
      slots: chart.api.addSeries(LWC.LineSeries, Object.assign({}, base, { visible: false })),
      low: chart.api.addSeries(LWC.LineSeries, band),
      high: chart.api.addSeries(LWC.LineSeries, band),
      line: chart.api.addSeries(LWC.LineSeries, line),
      label: null,
    };
    // The word on the chart itself, at the end of the line. A dot and a word, not an arrow: not a signal.
    chart.forecast.label = LWC.createSeriesMarkers(chart.forecast.line, [], { autoScale: false });
  }

  /* Open times of the candles a forecast made now is about: the forming candle and the three after it. */
  function forecastSlots() {
    if (chart.tf !== FORECAST_TF || chart.loadedTf !== FORECAST_TF || chart.bars.length === 0) {
      return null;
    }
    const last = chart.bars[chart.bars.length - 1];
    const first = last.closed === false ? last.time : last.time + STEP[FORECAST_TF];
    const out = [];
    for (let i = 0; i < FORECAST_POINTS; i += 1) {
      out.push(first + i * STEP[FORECAST_TF]);
    }
    return out;
  }

  /*
   * On the M15 chart the time axis always reaches to the end of those four candles, with a
   * forecast or without one. So drawing a forecast, or taking it away, adds no time to the
   * axis and removes none, and the candles stay where they are. Without this the chart
   * jumped three candles sideways each time. The values of this series are never drawn and
   * never read: it is not visible and has no part in the price scale.
   */
  function syncForecastSlots() {
    if (!chart.forecast) {
      return;
    }
    const slots = forecastSlots();
    const key = slots === null ? '' : chart.shift + '|' + slots[0];
    if (key === chart.slotsKey) {
      return;
    }
    chart.forecast.slots.setData(
      slots === null
        ? []
        : slots.map(function (time) {
            return { time: toChartTime(time), value: 0 };
          })
    );
    chart.slotsKey = key;
  }

  /*
   * The measured record, or null when a part of it is missing or cannot be true: then nothing
   * is shown as a forecast. The server refuses such a record too (backend/app/forecast.py);
   * the page checks again, because it prints these numbers under the line.
   */
  function forecastSkill(skill) {
    if (!skill || typeof skill !== 'object' || typeof skill.better_than_baseline !== 'boolean') {
      return null;
    }
    for (const name of ['samples', 'calls', 'calls_up', 'calls_down', 'train_rows']) {
      if (!Number.isInteger(skill[name]) || skill[name] < 0) {
        return null;
      }
    }
    for (const name of ['hit_rate', 'coverage', 'hit_rate_baseline']) {
      if (!isNum(skill[name]) || skill[name] < 0 || skill[name] > 1) {
        return null;
      }
    }
    for (const name of ['mae', 'baseline_mae']) {
      if (!isNum(skill[name]) || skill[name] < 0) {
        return null;
      }
    }
    if (skill.samples === 0 || skill.train_rows === 0 || skill.calls > skill.samples || skill.calls_up + skill.calls_down !== skill.calls) {
      return null;
    }
    if (!isNum(skill.from) || !isNum(skill.to) || !(skill.from > 0 && skill.from <= skill.to && skill.to < MAX_TIME)) {
      return null;
    }
    if (skill.scope !== FORECAST_SCOPE || (skill.hit_rate_baseline_call !== 'up' && skill.hit_rate_baseline_call !== 'down')) {
      return null;
    }
    if (skill.better_than_baseline && !(skill.mae < skill.baseline_mae && skill.calls > 0 && skill.hit_rate > 0.5)) {
      return null; // it claims an edge that its own numbers do not show
    }
    return skill;
  }

  /*
   * Whether an available block has the contract's shape: four points on the next four candles,
   * the band around the line, everything near the base price, a probability that is one, and a
   * direction word that agrees with the probability and with the line.
   */
  function forecastShape(p) {
    if (p.timeframe !== FORECAST_TF || p.horizon_candles !== FORECAST_POINTS || !isNum(p.made_at)) {
      return false;
    }
    if (!isNum(p.base_price) || !(p.base_price > 0) || !isNum(p.up_probability) || p.up_probability < 0 || p.up_probability > 1) {
      return false;
    }
    if (pick(DIRECTION_WORDS, p.direction) === undefined || !Array.isArray(p.points) || p.points.length !== FORECAST_POINTS) {
      return false;
    }
    const far = FORECAST_FAR * p.base_price;
    let time = p.made_at; // the first point is the candle that opened when the forecast was made
    for (const point of p.points) {
      if (!point || typeof point !== 'object' || point.time !== time) {
        return false;
      }
      if (!isNum(point.close) || !isNum(point.low) || !isNum(point.high) || point.low > point.close || point.close > point.high) {
        return false;
      }
      if (p.base_price - point.low > far || point.high - p.base_price > far) {
        return false;
      }
      time += STEP[FORECAST_TF];
    }
    const end = p.points[p.points.length - 1].close;
    if (p.direction === 'up' && !(p.up_probability > 0.5 && end > p.base_price)) {
      return false;
    }
    if (p.direction === 'down' && !(p.up_probability < 0.5 && end < p.base_price)) {
      return false;
    }
    return true;
  }

  /*
   * What to show now: {kind: 'drawn', p, skill}, {kind: 'other_tf'}, or {kind: 'off', why, skill}.
   * Every reason to show nothing is checked here, so a line can never outlive its moment.
   */
  function forecastView() {
    const p = state.snap && state.snap.prediction;
    const skill = p && typeof p === 'object' ? forecastSkill(p.skill) : null;
    const off = function (why) {
      return { kind: 'off', why: why, skill: skill };
    };
    if (!state.snap) {
      return off('loading');
    }
    if (chart.tf !== FORECAST_TF) {
      return { kind: 'other_tf' };
    }
    if (state.lost) {
      return off('lost');
    }
    if (state.tick && state.tick.feed === 'down') {
      return off('feed_down');
    }
    if (!p || typeof p !== 'object' || p.available !== true) {
      const reason = p && typeof p === 'object' ? p.reason : null;
      return off(reason === 'no_model' || reason === 'not_enough_candles' || reason === 'feed_down' ? reason : 'failed');
    }
    if (skill === null || !forecastShape(p) || state.timeframe !== FORECAST_TF) {
      return off('failed'); // no record or a broken block: not a forecast this page may show
    }
    if (!chart.series || !chart.forecast || chart.loadedTf !== FORECAST_TF || chart.bars.length === 0) {
      return off('chart');
    }
    if (chart.stale) {
      return off('waiting'); // a candle on screen missed its close: the candles are loaded again first
    }
    // The forecast must have been made on the close of the newest closed candle on screen.
    const bars = chart.bars;
    const last = bars[bars.length - 1];
    const closed = last.closed === false ? bars[bars.length - 2] : last;
    if (!closed || closed.closed === false || closed.time + STEP[FORECAST_TF] !== p.made_at) {
      return off('waiting');
    }
    if (last.closed === false && last.time !== p.made_at) {
      return off('waiting');
    }
    const now = estimatedNow();
    if (now !== null && now > p.made_at + STEP[FORECAST_TF] + FORECAST_LATE_S) {
      return off('waiting'); // the next candle is long overdue: this forecast belongs to the past
    }
    return { kind: 'drawn', p: p, skill: skill };
  }

  function clearForecastSeries() {
    if (!chart.forecast || chart.forecastKey === '') {
      return;
    }
    chart.forecastKey = '';
    chart.forecast.label.setMarkers([]);
    chart.forecast.line.setData([]);
    chart.forecast.low.setData([]);
    chart.forecast.high.setData([]);
  }

  function drawForecastSeries(p, dim) {
    const parts = [chart.shift, dim ? 1 : 0, p.made_at, p.base_price];
    for (const point of p.points) {
      parts.push(point.time, point.close, point.low, point.high);
    }
    const key = parts.join('|');
    if (key === chart.forecastKey) {
      return;
    }
    // All three start on the last closed candle's close, the price the forecast was made from.
    const start = toChartTime(p.made_at - STEP[FORECAST_TF]);
    const line = [{ time: start, value: p.base_price }];
    const low = [{ time: start, value: p.base_price }];
    const high = [{ time: start, value: p.base_price }];
    for (const point of p.points) {
      const time = toChartTime(point.time);
      line.push({ time: time, value: point.close });
      low.push({ time: time, value: point.low });
      high.push({ time: time, value: point.high });
    }
    chart.forecast.line.applyOptions({ color: dim ? FORECAST_COLOR.lineDim : FORECAST_COLOR.line });
    chart.forecast.low.applyOptions({ color: dim ? FORECAST_COLOR.bandDim : FORECAST_COLOR.band });
    chart.forecast.high.applyOptions({ color: dim ? FORECAST_COLOR.bandDim : FORECAST_COLOR.band });
    chart.forecast.line.setData(line);
    chart.forecast.low.setData(low);
    chart.forecast.high.setData(high);
    const end = p.points[p.points.length - 1];
    chart.forecast.label.setMarkers([
      {
        time: toChartTime(end.time),
        position: end.close < p.base_price ? 'belowBar' : 'aboveBar', // on the side the line does not come from
        shape: 'circle',
        size: 0.5,
        color: dim ? FORECAST_COLOR.lineDim : FORECAST_COLOR.line,
        text: 'Таамаг',
      },
    ]);
    chart.forecastKey = key;
  }

  function fmtDate(seconds) {
    return shifted(seconds).getUTCFullYear() + '.' + fmtDay(seconds);
  }

  function fmtPercent(share) {
    return (share * 100).toFixed(1) + '%';
  }

  function fmtCount(value) {
    return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /*
   * The measured record in words: the numbers of the walk-forward test, as the server sent
   * them. It says which hours were measured (the broker's open hours, by the rule of thumb),
   * and sets the hit rate beside what one constant call would have scored on the same calls.
   */
  function forecastRecordText(skill) {
    let calls = 'чиглэл хэлсэн таамаг нэг ч байгаагүй тул чиглэлийн үг хэр оновчтойг хэмжиж чадаагүй';
    if (skill.calls > 0) {
      const constant = skill.hit_rate_baseline_call === 'up' ? 'өсөх' : 'буурах';
      calls =
        'чиглэл хэлсэн ' + fmtCount(skill.calls) + ' таамгаас (өсөх ' + fmtCount(skill.calls_up) +
        ', буурах ' + fmtCount(skill.calls_down) + ') ' + fmtPercent(skill.hit_rate) +
        ' нь зөв байсан; тэр бүгдэд «' + constant + '» гэж хэлсэн бол ' + fmtPercent(skill.hit_rate_baseline) +
        ' нь зөв байх байсан';
    }
    // Two decimals as for prices; one or two more when the two errors would otherwise read the same.
    let digits = 2;
    while (digits < 4 && skill.mae !== skill.baseline_mae && skill.mae.toFixed(digits) === skill.baseline_mae.toFixed(digits)) {
      digits += 1;
    }
    return (
      'Туршилтын бүртгэл (' + fmtDate(skill.from) + ' ' + EN_DASH + ' ' + fmtDate(skill.to) +
      '). Модель урьд нь хараагүй ' + fmtCount(skill.samples) +
      ' лаан дээр, зөвхөн брокерын зах зээл нээлттэй гэж тооцсон цагт (ойролцоо цагийн хуваарь) хэмжсэн: ' + calls +
      '. 60 минутын дараах хаалтын дундаж алдаа ' + skill.mae.toFixed(digits) +
      '; «ханш байрандаа үлдэнэ» гэж үзэхэд ' + skill.baseline_mae.toFixed(digits) +
      '. Зурваст бодит хаалтын ' + fmtPercent(skill.coverage) + ' багтсан.'
    );
  }

  function forecastNodes(shown) {
    const nodes = [];
    if (shown.kind === 'other_tf') {
      nodes.push(el('p', 'forecast-line', 'Таамаг зөвхөн M15 дээр харагдана. Өөр хугацаанд хөрвүүлж зурдаггүй.'));
      return nodes;
    }
    const skill = shown.skill;
    if (shown.kind === 'drawn') {
      const p = shown.p;
      const end = p.points[p.points.length - 1];
      const now = estimatedNow();
      const main = el('p', 'forecast-line');
      main.append(
        'Чиглэл: ',
        el('strong', 'forecast-word', DIRECTION_WORDS[p.direction]),
        ' · өсөх магадлал ' + fmtPercent(p.up_probability) +
          (p.direction === 'flat' ? ' (зоос шидэхтэй ойролцоо тул чиглэл хэлэхгүй)' : '') +
          '. 60 минутын дараа, ' + fmtStamp(end.time + STEP[FORECAST_TF], now) + '-д хаагдах лааны хаалт: ',
        el('strong', 'forecast-num', fmtPrice(end.close)),
        ' орчим, зурвас ' + fmtPrice(end.low) + ' ' + EN_DASH + ' ' + fmtPrice(end.high) +
          ' (10 тохиолдлын 8-д хаалт энд багтахаар тооцсон). Суурь: ' + fmtStamp(p.made_at, now) +
          '-д хаагдсан лааны хаалт ' + fmtPrice(p.base_price) + '.'
      );
      nodes.push(main);
    } else {
      nodes.push(
        el('p', 'forecast-line', 'Таамаг бэлэн биш: ' + FORECAST_WHY[shown.why] + '.' + (pick(FORECAST_MORE, shown.why) || ''))
      );
    }
    if (skill) {
      nodes.push(el('p', 'forecast-line forecast-record', forecastRecordText(skill)));
      if (shown.kind === 'drawn' && shown.p.direction !== 'flat' && skill.calls === 0) {
        // The shown model can say a direction although no scored forecast ever did.
        nodes.push(
          noteNode(
            'warn',
            'warn',
            'Одоогийн «' + DIRECTION_WORDS[shown.p.direction] + '» гэсэн үг туршилтаар шалгагдаагүй: ' +
              'туршилтын үед модель чиглэл нэг ч удаа хэлээгүй. Энэ үгэнд найдах үндэслэл алга.'
          )
        );
      }
      if (!skill.better_than_baseline) {
        nodes.push(
          noteNode(
            'warn',
            'warn',
            'Туршилтаар энэ таамаг «ханш байрандаа үлдэнэ» гэж үзсэнээс илүү байгаагүй.' +
              (shown.kind === 'drawn' ? ' Тиймээс шугамыг бүдэг зурсан.' : '') +
              ' Үүнд тулгуурлаж шийдвэр гаргах хэрэггүй.'
          )
        );
      } else {
        nodes.push(
          el(
            'p',
            'forecast-line',
            'Туршилтаар энэ таамаг «ханш байрандаа үлдэнэ» гэж үзсэнээс бага алдаатай байсан. Цаашид ч тийм байх баталгаа биш.'
          )
        );
      }
    }
    return nodes;
  }

  /* The line on the chart, the legend entry and the words under the chart, all from one decision. */
  function syncForecast() {
    safe(syncForecastSlots);
    const shown = forecastView();
    const drawn = shown.kind === 'drawn';
    const dim = drawn && !shown.skill.better_than_baseline;
    safe(function () {
      if (drawn) {
        drawForecastSeries(shown.p, dim);
      } else {
        clearForecastSeries();
      }
    });

    let legend = 'Таамаг бэлэн биш';
    if (shown.kind === 'other_tf') {
      legend = 'Таамаг зөвхөн M15 дээр харагдана';
    } else if (drawn) {
      legend = dim ? 'Таамаг (туршилтаар давуу тал гараагүй)' : 'Таамаг (туршилтын)';
    } else {
      legend += ': ' + FORECAST_WHY[shown.why];
    }
    setText($('forecast-state'), legend);
    const swatch = $('forecast-swatch');
    if (swatch) {
      swatch.classList.toggle('is-dim', dim);
      swatch.classList.toggle('is-off', !drawn);
    }

    const body = $('forecast-body');
    if (!body) {
      return;
    }
    const now = estimatedNow();
    const key = JSON.stringify([
      shown.kind,
      shown.why || '',
      drawn ? shown.p : null,
      shown.skill || null,
      state.offset,
      now === null ? '' : dayIndex(now),
    ]);
    if (key !== view.forecastKey) {
      view.forecastKey = key;
      body.replaceChildren.apply(body, forecastNodes(shown));
    }
  }

  function pickTimeframe(tf) {
    if (!tfSupported(tf) || (tf === chart.tf && (chart.loadedTf === tf || chart.loading))) {
      return;
    }
    chart.tf = tf;
    writeStore(STORE.tf, tf);
    chart.info = null;
    chart.tailFails = 0;
    renderTimeframes();
    updateTitle();
    applyTimeScale();
    safe(renderChartNote);
    if (chart.series) {
      // Empty the series first: the other timeframe's candles must not sit under the new title.
      chart.loadedTf = null;
      chart.bars = [];
      chart.index = new Map();
      chart.hover = null;
      chart.series.setData([]);
      if (chart.markers) {
        chart.markers.setMarkers([]);
      }
      renderLegend();
      chartMessage('График ачаалж байна');
      loadCandles(true);
    }
    safe(syncForecast); // another timeframe, or M15 not loaded yet: no line until it fits again
  }

  // ------------------------------------------------------------ notifications

  function permission() {
    if (!('Notification' in window)) {
      return 'unsupported';
    }
    if (window.isSecureContext === false) {
      return 'insecure';
    }
    return window.Notification.permission;
  }

  function renderNotifyButton() {
    const button = $('notify-btn');
    const glyph = $('notify-icon');
    const state_ = notif.broken && permission() === 'granted' ? 'broken' : permission();
    const on = state_ === 'granted' && notif.enabled;
    let words = 'Мэдэгдэл асаах';
    let hint = 'Шинэ сигнал гарахад хөтчийн мэдэгдэл харуулна.';
    if (state_ === 'broken') {
      words = 'Энэ хөтөч мэдэгдэл харуулж чадахгүй';
      hint = 'Хөтөч зөвшөөрсөн ч мэдэгдлийг харуулсангүй (утасны хөтчид ийм байдаг). Компьютерын хөтөч ашиглана уу.';
    } else if (state_ === 'unsupported') {
      words = 'Хөтөч мэдэгдэл дэмжихгүй';
      hint = 'Энэ хөтөч мэдэгдэл харуулж чадахгүй.';
    } else if (state_ === 'insecure') {
      words = 'Мэдэгдэлд HTTPS хэрэгтэй';
      hint = 'Хөтчийн мэдэгдэл зөвхөн HTTPS эсвэл localhost хаягаар ажиллана.';
    } else if (state_ === 'denied') {
      words = 'Мэдэгдлийг хөтөч хориглосон';
      hint = 'Хөтчийн тохиргооноос энэ сайтад мэдэгдэл зөвшөөрнө үү.';
    } else if (on) {
      words = 'Мэдэгдэл асаалттай';
      hint = 'Дарвал мэдэгдлийг унтраана.';
    }
    setText($('notify-text'), words);
    button.title = hint;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.disabled = state_ === 'unsupported' || state_ === 'insecure' || state_ === 'broken';
    const name = on ? 'bell' : 'bellOff';
    if (glyph.getAttribute('data-icon') !== name) {
      glyph.setAttribute('data-icon', name);
      glyph.replaceChildren(icon(name, 18, 2));
    }
  }

  function renderSoundButton() {
    const button = $('sound-btn');
    const glyph = $('sound-icon');
    const words = notif.sound ? 'Дуут дохио асаалттай' : 'Дуут дохио унтраалттай';
    button.setAttribute('aria-pressed', notif.sound ? 'true' : 'false');
    button.setAttribute('aria-label', words);
    button.title = words;
    const name = notif.sound ? 'sound' : 'mute';
    if (glyph.getAttribute('data-icon') !== name) {
      glyph.setAttribute('data-icon', name);
      glyph.replaceChildren(icon(name, 18, 2));
    }
  }

  function askPermission() {
    return new Promise(function (resolve) {
      try {
        const result = window.Notification.requestPermission(resolve);
        if (result && typeof result.then === 'function') {
          result.then(resolve, function () {
            resolve(window.Notification.permission);
          });
        }
      } catch (error) {
        resolve('denied');
      }
    });
  }

  async function onNotifyClick() {
    const current = permission();
    if (current === 'unsupported' || current === 'insecure') {
      return;
    }
    if (current === 'granted') {
      notif.enabled = !notif.enabled;
    } else {
      const answer = await askPermission();
      notif.enabled = answer === 'granted' ? true : notif.enabled;
    }
    writeStore(STORE.notify, notif.enabled ? '1' : '0');
    renderNotifyButton();
    if (permission() === 'granted' && notif.enabled) {
      pushNotice('TradePilot AI', 'Мэдэгдэл асаалттай. Шинэ сигнал гарахад энд мэдэгдэнэ.', 'tradepilot-hello');
    }
  }

  function onSoundClick() {
    notif.sound = !notif.sound;
    writeStore(STORE.sound, notif.sound ? '1' : '0');
    renderSoundButton();
    if (notif.sound) {
      beep('signal');
    }
  }

  function pushNotice(title, body, tag, closeAfterMs) {
    if (permission() !== 'granted' || !notif.enabled) {
      return false;
    }
    try {
      const notice = new window.Notification(title, { body: body, tag: tag, icon: '/static/favicon.svg', lang: 'mn' });
      if (notif.broken) {
        // It works after all (another browser version, a setting changed): forget the mark.
        notif.broken = false;
        writeStore(STORE.broken, '0');
        safe(renderNotifyButton);
      }
      notice.onclick = function () {
        try {
          window.focus();
        } catch (error) {
          /* not allowed here */
        }
        notice.close();
      };
      if (isNum(closeAfterMs) && closeAfterMs > 0) {
        // A signal's notice must not outlive the signal.
        window.setTimeout(function () {
          notice.close();
        }, Math.min(closeAfterMs, 6 * 3600 * 1000));
      }
      return true;
    } catch (error) {
      // Some mobile browsers grant the permission and then allow notifications from a
      // service worker only. The page must not go on saying that notifications are on.
      if (!notif.broken) {
        notif.broken = true;
        writeStore(STORE.broken, '1'); // for the next visit: the button must not say "on" again
        safe(renderNotifyButton);
      }
      return false;
    }
  }

  /* A short beep made here (WebAudio): only after the user has touched the page and with sound on. */
  function beep(kind) {
    if (!notif.sound || !notif.interacted) {
      return;
    }
    try {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) {
        return;
      }
      if (!notif.audio) {
        notif.audio = new Ctor();
      }
      const audio = notif.audio;
      if (audio.state === 'suspended') {
        audio.resume();
      }
      const tones = kind === 'alert' ? [[440, 0, 0.28]] : [[880, 0, 0.14], [1174.66, 0.17, 0.2]];
      for (const tone of tones) {
        const start = audio.currentTime + 0.02 + tone[1];
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        oscillator.type = 'sine';
        oscillator.frequency.value = tone[0];
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.18, start + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + tone[2]);
        oscillator.connect(gain);
        gain.connect(audio.destination);
        oscillator.start(start);
        oscillator.stop(start + tone[2] + 0.03);
      }
    } catch (error) {
      /* no sound on this device */
    }
  }

  /*
   * A word in front of the tab's title while nobody looks. Two kinds, each with an end:
   * - a new signal ("BUY XAUUSD"): shown while the card is that signal, and dropped when the
   *   signal has expired by the clock. The clock runs without ticks too, so a lost server
   *   cannot leave the word there. Hidden, not dropped, while the card is something else for a
   *   moment (the feed-down card): it is back when the same signal is on the card again.
   * - a feed alert: "stopped" stays while the ticks say the feed is down; "back to normal"
   *   goes after ALERT_FLAG_MS.
   * The signal's word comes first: an alert never takes its place.
   */
  function looking() {
    return document.visibilityState === 'visible' && document.hasFocus();
  }

  function flagSignal(text, row) {
    if (looking() || !isNum(row.time) || !isNum(row.expires_at)) {
      return;
    }
    notif.signalFlag = { text: text, time: row.time, until: row.expires_at };
    paintTitle();
  }

  function flagAlert(text, level) {
    if (looking()) {
      return;
    }
    notif.alertFlag = { text: text, down: level === 'down', wall: Date.now(), mono: performance.now() };
    paintTitle();
  }

  function clearTitle() {
    notif.signalFlag = null;
    notif.alertFlag = null;
    paintTitle();
  }

  /* Drop the flags that have ended and write the title. Called on every tick and every second. */
  function paintTitle() {
    const tick = state.tick;
    let text = null;
    const signal = notif.signalFlag;
    if (signal) {
      // The server's clock carried on by whichever of this PC's two clocks moved further.
      const now = tick && isNum(tick.now) ? tick.now + idleMs() / 1000 : null;
      if (now === null || now >= signal.until) {
        notif.signalFlag = null;
      } else if (tick.card && tick.card.kind === 'signal' && tick.card.time === signal.time) {
        text = signal.text;
      }
    }
    const alert = notif.alertFlag;
    if (alert) {
      const ended = alert.down
        ? Boolean(tick) && tick.feed !== 'down'
        : Math.max(Date.now() - alert.wall, performance.now() - alert.mono) >= ALERT_FLAG_MS;
      if (ended) {
        notif.alertFlag = null;
      } else if (text === null) {
        text = alert.text;
      }
    }
    const title = text === null ? notif.titleBase : text + ' · ' + notif.titleBase;
    if (document.title !== title) {
      document.title = title;
    }
  }

  /* One signal, seen in the log of a snapshot or on the card of a tick: announced once, and only while it can be acted on. */
  function noteSignal(row, now) {
    const stored = Number(readStore(STORE.signal));
    const shownElsewhere = Number.isFinite(stored) && stored >= row.time;
    notif.lastSignal = row.time;
    writeStore(STORE.signal, row.time);
    if ((row.side === 'BUY' || row.side === 'SELL') && isNum(row.expires_at) && isNum(now) && row.expires_at > now) {
      signalNotice(row, now, !shownElsewhere);
    }
  }

  /*
   * While /api/state fails, the ticks still carry the signal card. A signal newer than the
   * last one seen is announced from that card, so a failing snapshot costs the log and the
   * reasons but not the notification. The snapshot that comes later finds it already seen.
   */
  function checkTickSignal() {
    const tick = state.tick;
    const card = tick && tick.card;
    if (!notif.baseline || snapshotJob.fails === 0 || state.lost || !card || card.kind !== 'signal') {
      return;
    }
    if (isNum(card.time) && card.time > notif.lastSignal) {
      noteSignal(card, tick.now);
    }
  }

  /*
   * A new signal: the newest log row's time is greater than the last one seen.
   * The first snapshot after the page loaded only sets that baseline. The
   * baseline is also kept in localStorage so a second open tab does not show
   * the same notification again.
   */
  function checkNotifications(snap) {
    const log = Array.isArray(snap.log) ? snap.log : [];
    const alerts = Array.isArray(snap.alerts) ? snap.alerts : [];
    const newest = log.length && log[0] && isNum(log[0].time) ? log[0] : null;
    let newestAlert = 0;
    for (const alert of alerts) {
      if (alert && isNum(alert.time) && alert.time > newestAlert) {
        newestAlert = alert.time;
      }
    }

    if (!notif.baseline) {
      notif.baseline = true;
      notif.lastSignal = newest ? newest.time : 0;
      notif.lastAlert = newestAlert;
      writeStore(STORE.signal, notif.lastSignal);
      return;
    }

    if (newest && newest.time > notif.lastSignal) {
      noteSignal(newest, isNum(snap.now) ? snap.now : estimatedNow());
    }

    if (newestAlert > notif.lastAlert) {
      const since = notif.lastAlert;
      notif.lastAlert = newestAlert;
      const fresh = alerts
        .filter(function (alert) {
          return alert && isNum(alert.time) && alert.time > since && (alert.level === 'down' || alert.code === 'feed_back');
        })
        .sort(function (a, b) {
          return a.time - b.time;
        });
      for (const alert of fresh) {
        const words = pick(LEVEL_WORDS, String(alert.level)) || String(alert.level);
        pushNotice('TradePilot · ' + words, String(alert.text || ''), 'tradepilot-alert-' + alert.time);
        flagAlert(words, alert.level);
      }
      if (fresh.length) {
        beep('alert');
      }
    }
  }

  function signalNotice(row, now, withPopup) {
    const dir = row.side === 'BUY' ? 1 : -1;
    const title = state.symbol + ' ' + row.side + ' · ' + String(row.grade === undefined || row.grade === null ? DASH : row.grade) + ' зэрэглэл';
    const body = [
      'Орох бүс: ' + fmtPrice(row.zone_low) + ' ' + EN_DASH + ' ' + fmtPrice(row.zone_high),
      'SL ' + fmtSigned(-dir * row.sl_distance) + ' · TP ' + fmtSigned(dir * row.tp_distance) + ' (орсон үнээс)',
      fmtClock(row.expires_at) + ' хүртэл хүчинтэй',
    ].join('\n');
    if (withPopup) {
      pushNotice(title, body, 'tradepilot-signal-' + row.time, (row.expires_at - now) * 1000);
    }
    beep('signal');
    flagSignal(row.side + ' ' + state.symbol, row);
  }

  // ---------------------------------------------------------------- UI wiring

  async function onEmailTest() {
    const button = $('email-test');
    button.disabled = true;
    setResult('Илгээж байна', '');
    try {
      const response = await request('/api/notify/test', { method: 'POST' });
      if (response.status === 409) {
        setResult('И-мэйл тохируулаагүй байна.', 'bad');
      } else if (!response.ok) {
        setResult('Илгээж чадсангүй (алдааны код ' + response.status + ').', 'bad');
      } else {
        const data = await response.json();
        if (data && data.ok === true) {
          setResult('Туршилтын и-мэйл илгээгдлээ.', 'ok');
        } else {
          setResult('И-мэйл илгээж чадсангүй. SMTP тохиргоог шалгана уу.', 'bad');
        }
      }
    } catch (error) {
      if (!(error instanceof AuthError)) {
        setResult('Сервертэй холбогдож чадсангүй.', 'bad');
      }
    } finally {
      button.disabled = false;
    }
  }

  async function onLogout() {
    const button = $('logout-btn');
    const words = 'Системээс гарах';
    button.disabled = true;
    try {
      const response = await fetch('/api/logout', { method: 'POST', credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) {
        throw new Error('HTTP ' + response.status);
      }
    } catch (error) {
      button.disabled = false;
      setText(button, 'Гарч чадсангүй');
      window.setTimeout(function () {
        setText(button, words);
      }, 3000);
      return;
    }
    state.leaving = true;
    closeStream();
    if (state.leaveTimer !== null) {
      // The session had ended and the page was about to leave by itself: one navigation, this one.
      window.clearTimeout(state.leaveTimer);
      state.leaveTimer = null;
    }
    window.location.assign('/login');
  }

  function initRail() {
    const links = Array.prototype.slice.call(document.querySelectorAll('[data-spy]'));
    const targets = links
      .map(function (link) {
        return { link: link, section: $(link.getAttribute('data-spy')) };
      })
      .filter(function (item) {
        return item.section;
      });
    if (!targets.length) {
      return;
    }
    let queued = false;
    const paint = function () {
      queued = false;
      let active = targets[0];
      if (window.scrollY > 40) {
        const line = window.innerHeight * 0.3;
        let best = -Infinity;
        for (const item of targets) {
          const top = item.section.getBoundingClientRect().top;
          if (top <= line && top > best) {
            best = top;
            active = item;
          }
        }
      }
      for (const item of targets) {
        if (item === active) {
          item.link.setAttribute('aria-current', 'true');
        } else {
          item.link.removeAttribute('aria-current');
        }
      }
    };
    const ask = function () {
      if (!queued) {
        queued = true;
        window.requestAnimationFrame(paint);
      }
    };
    window.addEventListener('scroll', ask, { passive: true });
    window.addEventListener('resize', ask, { passive: true });
    paint();
  }

  function wire() {
    wireTimeframes();
    $('notify-btn').addEventListener('click', onNotifyClick);
    $('sound-btn').addEventListener('click', onSoundClick);
    $('logout-btn').addEventListener('click', onLogout);
    $('email-test').addEventListener('click', onEmailTest);

    const touched = function () {
      notif.interacted = true;
      window.removeEventListener('pointerdown', touched, true);
      window.removeEventListener('keydown', touched, true);
    };
    window.addEventListener('pointerdown', touched, true);
    window.addEventListener('keydown', touched, true);

    window.addEventListener('focus', clearTitle);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible') {
        return;
      }
      if (document.hasFocus()) {
        clearTitle();
      }
      requestCandles(); // the page was away: load the candles again
      if (state.tick && state.tick.v !== state.snapV) {
        refreshSnapshot();
      }
      checkStream();
      safe(renderNotifyButton);
    });
    window.addEventListener('online', checkStream);
    window.addEventListener('pagehide', function (event) {
      if (!event.persisted) {
        closeStream();
      }
    });
    window.addEventListener('pageshow', function (event) {
      if (event.persisted && !state.leaving) {
        stream.broken = true;
        checkStream();
      }
    });
  }

  // --------------------------------------------------------------------- boot

  async function boot() {
    wire();
    safe(renderNotifyButton);
    safe(renderSoundButton);
    safe(initRail);
    safe(initChart);
    safe(initTimeframes);
    window.setInterval(watchdog, 1000);

    const first = refreshSnapshot(); // GET /api/state
    loadCandles(true); // GET /api/candles for the timeframe picked last time, at the same time
    await first;
    openStream(); // then the live stream
  }

  boot();
})();
