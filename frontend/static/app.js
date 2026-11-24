/*
 * TradePilot AI: the dashboard page.
 *
 * Shows what the API returns (docs/WEB.md): the snapshot of GET /api/state,
 * the candles of GET /api/candles, the "tick" events of GET /api/stream and the
 * chart's own "candles" events of GET /api/candles/stream (the chart only:
 * signals never depend on it).
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
  const TAIL_BARS = 3; // newest candles asked for while the chart's own stream is silent (not on M15)
  const TAIL_EVERY_MS = 1500;
  const TAIL_FAILS_SHOWN = 3;
  // The chart's own candle stream (GET /api/candles/stream, docs/WEB.md). It sends an event at
  // least every 5 s; silent for longer than LIVE_SILENT_MS it no longer draws, and the tick (M15)
  // or the tail requests (other timeframes) draw instead, as without it.
  const NARROW_CHART_PX = 480; // a chart narrower than this (a phone) keeps its price axis narrow
  const HIDDEN_CLOSE_MS = 10000; // a page hidden this long gives its chart stream back (awayLong)
  const LIVE_SILENT_MS = 8000;
  const LIVE_RESTART_MS = 20000; // an open chart stream that stays silent this long is opened again
  const LIVE_PAUSES_MS = [2000, 5000, 10000, 30000]; // before the chart stream is opened again after an error
  const LIVE_LATE_S = 10; // the newest confirmation of the candles is older than this: "late"
  const LIVE_DOWN_S = 60; // ... older than this: "down"
  const FALLBACK_FRESH_MS = 10000; // the tick or a tail answer drew within this: the chart is "by polling"
  const FIGURE_SPACE = ' '; // as wide as a digit: keeps the price label one width while it counts down
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
    page: '#0B1220',
    // Volume bars: the candle's colour, dimmer than the candles; dimmer still while it forms.
    upVolume: 'rgba(45, 212, 191, 0.30)',
    downVolume: 'rgba(251, 113, 133, 0.30)',
    upVolumeSoft: 'rgba(45, 212, 191, 0.16)',
    downVolumeSoft: 'rgba(251, 113, 133, 0.16)',
  };
  // Why a menu timeframe cannot be shown (snapshot.chart.missing), in words for its row.
  const MISSING_WORDS = {
    no_shorter_candles: function (shortest) {
      return 'Энэ эх үүсвэрт ' + shortest + ablative(shortest) + ' богино лаа байхгүй';
    },
    cannot_build: function () {
      return 'Энэ эх үүсвэрийн лаагаар бүтээх боломжгүй';
    },
  };
  // The chart's own candle stream, as its state chip says it. Not the feed's status (header).
  const LIVE_WORDS = {
    wait: 'холбогдож байна',
    live: 'шууд урсгал',
    poll: 'асуулгаар шинэчилж байна',
    late: 'хоцорч байна',
    down: 'тасарсан',
  };
  const LIVE_HINTS = {
    wait: 'Графикийн лааны урсгалд холбогдож байна.',
    live: 'Лаа эх үүсвэрийн шууд урсгалаас (WebSocket) шинэчлэгдэж байна.',
    poll: 'Лаа сервер эх үүсвэрээс давтан асууж шинэчилж байна (шууд урсгалгүй эсвэл түр тасарсан).',
    late: 'Сүүлийн шинэчлэлээс хойш удаж байна. Сүүлийн лаа одоогийн ханш биш байж магадгүй.',
    down: 'Графикийн лаа шинэчлэгдэхгүй байна. Сүүлийн лаа одоогийн ханш биш.',
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
  // The server's rule for "better than the baseline" (core/forecast.judge), as far as a record shows it.
  const FORECAST_MIN_CALLS = 300;
  const FORECAST_Z95 = 1.96;

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
    volume: null, // the volume bars under the candles (their own price scale at the bottom)
    label: null, // the price-axis label of the newest candle: its close and the time left (initPriceLabel)
    tf: storedTimeframe(), // the timeframe the user picked: a way of looking, signals do not depend on it
    loadedTf: null, // the timeframe whose candles are in the series
    bars: [], // those candles, times in UTC seconds; closed: false marks the forming one
    index: new Map(), // time -> position in bars
    last: {}, // last bar time per timeframe
    favorites: storedFavorites(), // the starred timeframes: quick buttons above the chart
    supported: null, // the timeframes the server can show; null until a snapshot says
    missing: {}, // timeframe -> reason code, for those the server cannot show (snapshot.chart.missing)
    shortest: '', // the source's shortest candle (snapshot.chart.shortest)
    supportKey: '',
    rev: null, // the history counter that came with the candles on screen (/api/candles rev)
    retryAt: 0, // Date.now() before which the candles are not asked for again (a 503's Retry-After)
    unavailable: null, // {until}: the source gives nothing right now (a 503, or the stream's error)
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
    forecastShown: null, // {p, dim}: the forecast drawn now, for placing its word again
    wordKey: '', // where the word «Таамаг» is now; '' when it is not drawn
    slotsKey: '', // which future candles the time axis reaches to; '' when none
    anchored: null, // whether a zoom keeps the right edge (the newest candle in view); see updateFollowButton
    narrow: false, // the chart is narrow (a phone): the axis label shows the close only, the time left is in the legend
  };

  const stream = { source: null, broken: false, timer: null, delay: 2000, restartedAt: 0 };

  /*
   * The chart's own candle stream: one EventSource for the timeframe on screen. While it delivers
   * it is the chart's only writer; while it is silent or broken the tick (M15) and the tail
   * requests (other timeframes) draw, as they did without it.
   */
  const live = {
    started: false, // boot has opened the streams: before that nothing opens this one
    source: null,
    tf: null, // the timeframe of that source
    timer: null, // the pause before it is opened again after an error
    pause: 0, // index into LIVE_PAUSES_MS
    openedAt: 0,
    restartedAt: 0,
    lastWall: 0, // Date.now() when its last event arrived
    data: null, // that event: {mode, at, age, error, retryAfter, bars}
    gaps: null, // its gap count, to compare with the next event's
    fallbackWall: 0, // Date.now() when the tick or a tail answer last drew while the stream was silent
    fallbackAt: null, // the server's time of that
    stateKey: '',
    hiddenSince: document.hidden ? Date.now() : null, // Date.now() when the page was last hidden; null while shown
  };

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

  /* The ablative ending ("-аас" = than / from) after a timeframe code, by how its last number is read. */
  function ablative(code) {
    const endings = ['аас', 'ээс', 'оос', 'аас', 'өөс', 'аас', 'аас', 'оос', 'аас', 'өөс'];
    const digit = /(\d)$/.exec(String(code));
    return '-' + (digit ? endings[Number(digit[1])] : 'аас');
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

  /* "16:30:05", with the date in front when the moment is not on the same day as ``now``. */
  function fmtStampSec(seconds, now) {
    if (!isNum(seconds)) {
      return DASH;
    }
    const d = shifted(seconds);
    const clock = pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ':' + pad2(d.getUTCSeconds());
    return isNum(now) && dayIndex(seconds) !== dayIndex(now) ? fmtDay(seconds) + ' ' + clock : clock;
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

  /* A candle's volume as the source gave it (base units); a dash when it gave none. */
  function fmtVolume(value) {
    if (!isNum(value)) {
      return DASH;
    }
    if (value >= 10000) {
      return String(Math.round(value)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }
    return value.toFixed(value >= 100 ? 1 : 2);
  }

  /*
   * The time left until a candle of ``tf`` closes, in one width per timeframe (so the price
   * label does not change width while it counts): mm:ss up to H1, h:mm:ss up to D1, days for
   * weeks and months (h:mm:ss in their last day). Zero when the time is up.
   */
  function fmtLeft(left, tf) {
    const total = Math.max(0, Math.ceil(left));
    const months = pick(MONTHS, tf);
    const step = months === undefined ? STEP[tf] : null;
    if (step === null || tf === 'W1') {
      if (total >= 86400) {
        const width = { W1: 1, MN1: 2, MN3: 2, MN6: 3, MN12: 3 }[tf] || 1;
        return String(Math.floor(total / 86400)).padStart(width, FIGURE_SPACE) + ' хоног';
      }
    }
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    if (step !== null && step <= 3600) {
      return pad2(minutes) + ':' + pad2(seconds);
    }
    const hourDigits = step !== null && step < 86400 ? String(Math.floor(step / 3600)).length : 2;
    return String(Math.floor(total / 3600)).padStart(hourDigits, '0') + ':' + pad2(minutes % 60) + ':' + pad2(seconds);
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
    closeChartStream();
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
        const failure = new Error('HTTP ' + response.status);
        failure.status = response.status;
        const wait = Number(response.headers && response.headers.get ? response.headers.get('Retry-After') : NaN);
        failure.retryAfter = isNum(wait) && wait >= 0 ? wait : null; // a 503 says when to ask again
        throw failure;
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
    safe(checkChartStream);
    safe(renderLiveState); // live / by polling / late / down, judged by the clock as well
    safe(updatePriceLabel); // the time left until the newest candle closes
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
    const own = liveState().kind; // the chart's own candles: another timeframe can move while the feed is down
    if (state.lost && tick) {
      notes.push(['down', 'warn', 'Сервертэй холбоо тасарсан. График шинэчлэгдэхгүй байна.']);
    } else if (tick && tick.feed === 'down' && (chart.tf === state.timeframe || own === 'late' || own === 'down')) {
      notes.push(['down', 'warn', 'Ханш тасарсан. Сүүлийн лаа шинэчлэгдэхгүй байна.']);
    } else if (chart.unavailable) {
      // A 503 or the stream's "unavailable": the source gives nothing right now. Not "too short a history".
      notes.push([
        'down',
        'warn',
        'Эх үүсвэр ' + chart.tf + ' лааг одоогоор өгөхгүй байна (түр саатал).' +
          (chart.bars.length ? ' Дэлгэц дээрх сүүлийн лаа шинэчлэгдэхгүй байна.' : '') +
          ' Дахин оролдож байна.',
      ]);
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

  /*
   * A bar as the page keeps it. Only the server's own `closed: false` makes a candle a forming one.
   * Volume: a number, or null when the source gave none (the tick's bars carry none).
   */
  function barOf(row) {
    return {
      time: row.time,
      open: row.open,
      high: row.high,
      low: row.low,
      close: row.close,
      volume: isNum(row.volume) && row.volume >= 0 ? row.volume : null,
      closed: row.closed !== false,
    };
  }

  /* The volume bar of a candle; a candle without volume data gets none (a gap, not a zero). */
  function volumeBar(bar) {
    if (bar.volume === null) {
      return { time: toChartTime(bar.time) };
    }
    const rising = bar.close >= bar.open;
    let color = rising ? COLOR.upVolume : COLOR.downVolume;
    if (bar.closed === false) {
      color = rising ? COLOR.upVolumeSoft : COLOR.downVolumeSoft;
    }
    return { time: toChartTime(bar.time), value: bar.volume, color: color };
  }

  /*
   * A row of an update as a bar. A row without a volume field (a tick from a server older than this
   * page) keeps the volume the page holds for that candle: no field means "not said", not "none".
   */
  function updateBar(row) {
    const bar = barOf(row);
    if (!Object.prototype.hasOwnProperty.call(row, 'volume')) {
      const at = chart.index.get(row.time);
      if (at !== undefined) {
        bar.volume = chart.bars[at].volume;
      }
    }
    return bar;
  }

  /* Whether a kept bar and a bar of an update differ in anything that is drawn. */
  function barDiffers(kept, bar) {
    return (
      kept.open !== bar.open ||
      kept.high !== bar.high ||
      kept.low !== bar.low ||
      kept.close !== bar.close ||
      kept.volume !== bar.volume ||
      kept.closed !== bar.closed
    );
  }

  /*
   * The volume bars: a histogram on a scale of its own in the lowest fifth of the chart, so it
   * never changes the candles' price range. No label and no line on the price axis.
   */
  function initVolume(LWC) {
    chart.volume = chart.api.addSeries(LWC.HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
      priceLineVisible: false,
      lastValueVisible: false,
      base: 0,
    });
    chart.api.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  }

  /*
   * The newest candle's label on the right price axis: its close and the time left until it
   * closes ("3626.03 · 00:22"), in place of the library's own last-value label. A series
   * primitive's axis view: the library places it, moves it clear of the TP / SL labels and makes
   * the axis wide enough for it. Coloured like the candle while the chart's candles are current,
   * grey while they are late or down.
   */
  function initPriceLabel() {
    const label = { series: null, redraw: null, price: null, text: '', back: COLOR.raised, color: COLOR.dim, key: '' };
    const view = {
      coordinate: function () {
        const y = label.series && label.price !== null ? label.series.priceToCoordinate(label.price) : null;
        return isNum(y) ? y : -1000;
      },
      text: function () {
        return label.text;
      },
      textColor: function () {
        return label.color;
      },
      backColor: function () {
        return label.back;
      },
      visible: function () {
        return label.text !== '' && label.price !== null;
      },
      tickVisible: function () {
        return true;
      },
    };
    const views = [view]; // the same array every time: the library caches by it
    label.primitive = {
      attached: function (param) {
        label.series = param.series;
        label.redraw = param.requestUpdate;
      },
      detached: function () {
        label.series = null;
        label.redraw = null;
      },
      updateAllViews: function () {},
      priceAxisViews: function () {
        return views;
      },
    };
    chart.series.attachPrimitive(label.primitive);
    chart.series.applyOptions({ lastValueVisible: false }); // this label takes the place of the library's
    chart.label = label;
  }

  /* The label's text and colour now; asked again every second for the countdown. */
  function updatePriceLabel() {
    const label = chart.label;
    if (!label) {
      return;
    }
    const last = chart.loadedTf === chart.tf && chart.bars.length ? chart.bars[chart.bars.length - 1] : null;
    let text = '';
    let back = COLOR.raised;
    let color = COLOR.dim;
    // On a narrow chart (a phone) the label with the time left would take a third of the width:
    // the axis then shows the close only, and the time left goes into the O H L C row.
    const host = $('chart-el');
    const narrow = Boolean(host) && host.clientWidth > 0 && host.clientWidth < NARROW_CHART_PX;
    let leftText = '';
    if (last) {
      const now = estimatedNow();
      // A closed newest candle has no time left: zeros, in the same width, until the next one comes.
      const left = now === null ? null : last.closed === false ? nextOpen(last.time, chart.tf) - now : 0;
      leftText = left === null ? '' : fmtLeft(left, chart.tf);
      text = fmtPrice(last.close) + (leftText === '' || narrow ? '' : ' · ' + leftText);
      const kind = liveState().kind;
      if (kind === 'live' || kind === 'poll') {
        back = last.close >= last.open ? COLOR.up : COLOR.down;
        color = COLOR.page;
      }
    }
    label.price = last ? last.close : null;
    chart.narrow = narrow;
    const leftNode = $('ohlc-left');
    if (leftNode) {
      setText(leftNode, narrow && leftText !== '' ? 'хаагдах хүртэл ' + leftText : '');
      show(leftNode, narrow && leftText !== '');
    }
    const key = text + '|' + back + '|' + label.price;
    if (key === label.key) {
      return;
    }
    label.key = key;
    label.text = text;
    label.back = back;
    label.color = color;
    if (label.redraw) {
      label.redraw();
    }
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
        // The candles keep the lower fifth for the volume bars, which have a scale of their own.
        rightPriceScale: { borderColor: COLOR.border, scaleMargins: { top: 0.1, bottom: 0.22 } },
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
        // As on TradingView (the owner asked for it): the plain mouse wheel over the chart zooms it,
        // a sideways wheel or a drag moves it, a pinch zooms on a phone. A vertical swipe on a
        // phone still scrolls the page; the chart takes horizontal ones.
        handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
        handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
      });
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
      // Added after the forecast's series, each on its own: a failure here costs that part only.
      safe(function () {
        initVolume(LWC);
      });
      safe(initPriceLabel);
      safe(function () {
        // Whether the newest candle is in view decides the «Шууд ханш руу» button.
        chart.api.timeScale().subscribeVisibleLogicalRangeChange(function () {
          safe(updateFollowButton);
        });
      });
    } catch (error) {
      chart.api = null;
      chart.series = null;
      chart.markers = null;
      chart.volume = null;
      chart.label = null;
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
      chart.rev = body && isNum(body.rev) ? body.rev : null; // the chart stream's `rev` is compared with it
      chart.tailFails = 0;
      chart.unavailable = null;
      setBars(tf, bars, resetView);
      safe(renderChartNote);
    } catch (error) {
      if (token === chart.request) {
        if (error && error.status === 503) {
          // Nothing right now: asked again after the server's Retry-After, and said in words.
          const wait = isNum(error.retryAfter) ? Math.min(Math.max(error.retryAfter, 1), 60) : 5;
          chart.retryAt = Date.now() + wait * 1000;
          chart.unavailable = { until: chart.retryAt };
        }
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
          chartMessage(
            error && error.status === 503
              ? 'Эх үүсвэр ' + tf + ' лааг одоогоор өгөхгүй байна (түр саатал). Дахин оролдож байна.'
              : 'Лааны түүхийг татаж чадсангүй. Дахин оролдож байна.'
          );
        }
        safe(renderChartNote);
      }
    } finally {
      if (token === chart.request) {
        chart.loading = false;
      }
    }
  }

  /* The guarded form: used by the gap check, a stream reconnect and a page that became visible. */
  function requestCandles() {
    if (!chart.series || chart.loading || Date.now() - chart.startedAt < CANDLE_PAUSE_MS || Date.now() < chart.retryAt) {
      return;
    }
    loadCandles(chart.loadedTf !== chart.tf); // nothing on screen yet: start at the newest candle
  }

  function setBars(tf, bars, resetView) {
    // A reload of the same timeframe (a gap, a moved history counter, a new display zone) keeps
    // what the user looks at: the same candles in view at the same zoom.
    let keep = null;
    if (!resetView && chart.loadedTf === tf) {
      safe(function () {
        keep = captureView();
      });
    }
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
    safe(function () {
      if (chart.volume) {
        chart.volume.setData(bars.map(volumeBar));
      }
    });
    safe(syncForecastSlots); // before the view is reset: the time axis is complete then
    if (resetView && bars.length) {
      chart.api.timeScale().resetTimeScale(); // the newest candles at the default zoom
    } else if (keep) {
      safe(function () {
        restoreView(keep);
      });
    }
    safe(updatePriceLabel);
    safe(updateFollowButton);
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
   * The newest bars of a tick, used only while the chart's own stream is silent or broken (one
   * writer). One origin per timeframe: the tick's M15 bars are the feed's own candles, as is the
   * M15 history, so they may be drawn on M15. Its H1 bars are the filter's view, built from the
   * feed's M15 candles, while the H1 history comes from the source's own 1h candles: they are
   * never drawn. Any timeframe but M15 asks for its newest candles instead (requestTail).
   */
  function applyBars(tickBars) {
    if (!chart.series || !tickBars || typeof tickBars !== 'object' || liveDrawing()) {
      return;
    }
    const tf = chart.tf;
    if (tf === state.timeframe) {
      if (Array.isArray(tickBars[tf])) {
        applyList(tf, tickBars[tf], 'tick');
      }
      // ({} while the feed is down: nothing moves then.)
    } else if (Object.keys(tickBars).length > 0) {
      // ({} while the feed is down: nothing moves then.)
      if (chart.loadedTf !== tf) {
        requestCandles(); // still on their way, or an earlier load failed: then ask again
      } else {
        requestTail();
      }
    }
  }

  /* The newest candles of a timeframe while its chart stream is silent. One request at a time, never a queue. */
  async function requestTail() {
    const tf = chart.tf;
    // Not while the page has been hidden for a while: nobody looks, and the history is loaded again when it shows.
    if (chart.tailBusy || chart.loading || state.leaving || chart.loadedTf !== tf || awayLong()) {
      return;
    }
    if (chart.stale) {
      requestCandles(); // a candle on screen missed its close: the whole history, not a tail
      return;
    }
    if (Date.now() - chart.tailAt < TAIL_EVERY_MS || Date.now() < chart.retryAt) {
      return;
    }
    chart.tailBusy = true;
    chart.tailAt = Date.now();
    const token = chart.request; // a new choice or a reload makes this answer an old one
    try {
      const body = await getJSON('/api/candles?timeframe=' + encodeURIComponent(tf) + '&limit=' + TAIL_BARS);
      if (token === chart.request && !chart.loading && chart.loadedTf === tf && !liveDrawing()) {
        chart.tailFails = 0;
        chart.unavailable = null;
        if (isNum(body && body.rev) && isNum(chart.rev) && body.rev !== chart.rev) {
          chart.stale = true; // older candles changed on the server: the whole history again
        }
        applyList(tf, body && Array.isArray(body.bars) ? body.bars : [], 'tail');
      }
    } catch (error) {
      if (token === chart.request) {
        chart.tailFails += 1;
        if (error && error.status === 503) {
          const wait = isNum(error.retryAfter) ? Math.min(Math.max(error.retryAfter, 1), 60) : 5;
          chart.retryAt = Date.now() + wait * 1000;
          chart.unavailable = { until: chart.retryAt };
        }
      }
    } finally {
      chart.tailBusy = false;
      safe(renderChartNote);
    }
  }

  /*
   * Bars of the chart stream, a tick or a tail request (``via``: 'stream', 'tick', 'tail'), oldest
   * first. The candle on screen and the next one are drawn with update(): the forming candle moves
   * in place, a candle that closes keeps its final values and the next one is added once. An
   * older candle of the list may only get its final values (a closed one whose values were
   * confirmed after the close); it never becomes a forming one again. A list that starts after
   * the candle on screen, or jumps over a candle, is a missed close: the whole history is loaded
   * again.
   */
  function applyList(tf, list, via) {
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
    let added = false;
    for (const row of list) {
      if (!validBar(row)) {
        continue;
      }
      if (row.time < last) {
        const at = chart.index.get(row.time);
        const bar = updateBar(row);
        if (at !== undefined && row.closed !== false && barDiffers(chart.bars[at], bar)) {
          try {
            chart.series.update(chartBar(bar), true); // an existing candle that is not the newest
            if (chart.volume) {
              chart.volume.update(volumeBar(bar), true);
            }
          } catch (error) {
            reload = true;
            break;
          }
          chart.bars[at] = bar;
        }
        continue;
      }
      if (row.time > nextOpen(last, tf)) {
        reload = true; // a gap: more than one candle ahead
        break;
      }
      const bar = updateBar(row);
      const isNew = !chart.index.has(bar.time);
      try {
        chart.series.update(chartBar(bar));
        if (chart.volume) {
          chart.volume.update(volumeBar(bar));
        }
      } catch (error) {
        reload = true;
        break;
      }
      rememberBar(bar);
      last = bar.time;
      chart.last[tf] = last;
      added = added || isNew;
    }
    if (via !== 'stream') {
      live.fallbackWall = Date.now(); // the chart moves by the tick or by tail requests now
      live.fallbackAt = via === 'tick' && state.tick && isNum(state.tick.now) ? state.tick.now : estimatedNow();
    }
    if (reload) {
      chart.stale = true;
    }
    if (chart.stale) {
      requestCandles(); // asked again on every update until a load succeeds
    }
    if (added) {
      safe(updateMarkers); // a signal's marker may sit on the candle that just opened
      safe(updateFollowButton);
    }
    if (chart.hover === null) {
      renderLegend();
    }
    safe(updatePriceLabel);
  }

  // ------------------------------------------------- the chart's own candle stream

  /* Whether the chart stream delivers now for the timeframe on screen: then it alone draws. */
  function liveDrawing() {
    const source = live.source;
    return (
      Boolean(source) &&
      typeof window.EventSource === 'function' &&
      source.readyState === window.EventSource.OPEN &&
      live.tf === chart.tf &&
      live.data !== null &&
      live.data.error === null &&
      live.data.bars.length > 0 &&
      Date.now() - live.lastWall < LIVE_SILENT_MS
    );
  }

  /*
   * The state of the chart's own candles, apart from the feed's status in the header:
   * {kind: 'wait' | 'live' | 'poll' | 'late' | 'down', at: the source's time of the newest update or null}.
   */
  function liveState() {
    const data = live.data;
    const now = Date.now();
    const at = data && data.tf === chart.tf ? data.at : null;
    if (state.lost) {
      return { kind: 'down', at: newestAt(at) };
    }
    if (data && data.tf === chart.tf && now - live.lastWall < LIVE_SILENT_MS && live.source && live.tf === chart.tf) {
      if (data.error !== null) {
        return { kind: 'down', at: at };
      }
      if (data.age === null) {
        return { kind: 'wait', at: at };
      }
      const age = data.age + (now - live.lastWall) / 1000;
      if (age > LIVE_DOWN_S) {
        return { kind: 'down', at: at };
      }
      if (age > LIVE_LATE_S) {
        return { kind: 'late', at: at };
      }
      return { kind: data.mode === 'stream' ? 'live' : 'poll', at: at };
    }
    // The chart stream is silent or broken: the tick (M15) or tail requests keep the chart.
    const fallback = live.fallbackWall > 0 && chart.loadedTf === chart.tf;
    if (fallback && now - live.fallbackWall < FALLBACK_FRESH_MS && !chart.unavailable) {
      return { kind: 'poll', at: newestAt(at) };
    }
    if (!fallback && !(data && data.tf === chart.tf)) {
      return { kind: chart.unavailable ? 'down' : 'wait', at: null };
    }
    const quiet = now - Math.max(live.lastWall, live.fallbackWall);
    const down = chart.unavailable || chart.tailFails >= TAIL_FAILS_SHOWN || quiet > LIVE_DOWN_S * 1000;
    return { kind: down ? 'down' : 'late', at: newestAt(at) };
  }

  function newestAt(at) {
    const fallback = chart.loadedTf === chart.tf ? live.fallbackAt : null;
    if (isNum(at) && isNum(fallback)) {
      return Math.max(at, fallback);
    }
    return isNum(at) ? at : isNum(fallback) ? fallback : null;
  }

  /* The chip beside the chart's title: the real source and symbol, and the state of its candles. */
  function renderLiveState() {
    const chip = $('chart-live');
    if (!chip) {
      return;
    }
    const now = estimatedNow();
    const seen = liveState();
    let words = LIVE_WORDS[seen.kind];
    if (seen.kind !== 'live' && seen.kind !== 'wait' && isNum(seen.at)) {
      words += ' · сүүлд ' + fmtStampSec(seen.at, now);
    }
    const text = sourceFull() + ' · ' + words;
    const key = seen.kind + '|' + text;
    if (key === live.stateKey) {
      return;
    }
    live.stateKey = key;
    const className = 'live-chip is-' + seen.kind;
    if (chip.className !== className) {
      chip.className = className;
    }
    setText($('chart-live-text'), text);
    chip.title = LIVE_HINTS[seen.kind];
    safe(updatePriceLabel); // its colour follows the state
    safe(renderChartNote);
  }

  /* One chart stream event (docs/WEB.md "The chart's live stream"). Only the newest choice's are used. */
  function onChartEvent(source, raw) {
    if (source !== live.source) {
      return;
    }
    let data;
    try {
      data = JSON.parse(raw);
    } catch (error) {
      return;
    }
    if (!data || typeof data !== 'object' || data.timeframe !== live.tf || live.tf !== chart.tf) {
      return;
    }
    const bars = Array.isArray(data.bars) ? data.bars.filter(validBar) : [];
    if (bars.some(function (bar) { return candleOpen(bar.time, live.tf) !== bar.time; })) {
      return; // not candles of this timeframe: nothing of another origin is drawn under its title
    }
    const error = data.error === null || data.error === undefined ? null : String(data.error);
    live.pause = 0;
    live.lastWall = Date.now();
    live.data = {
      tf: live.tf,
      mode: data.mode === 'stream' ? 'stream' : 'poll',
      at: isNum(data.at) ? data.at : null,
      age: isNum(data.age) ? data.age : null,
      error: error,
      retryAfter: isNum(data.retry_after) ? data.retry_after : null,
      bars: bars,
    };
    if (error !== null) {
      // The source gives nothing right now: nothing is drawn as current, and it is said in words.
      const wait = live.data.retryAfter !== null ? Math.min(Math.max(live.data.retryAfter, 1), 60) : 5;
      chart.unavailable = { until: Date.now() + wait * 1000 };
    } else {
      chart.unavailable = null;
      chart.tailFails = 0;
      // Candles older than the newest three changed on the server (a refill after a reconnect, a
      // reload, a hole that healed), or the number of holes did: the history is loaded again, with
      // the view kept. Until a load succeeds every event asks again (chart.stale).
      if (chart.loadedTf === live.tf) {
        if (isNum(data.rev) && isNum(chart.rev) && data.rev !== chart.rev) {
          chart.stale = true;
        }
        if (isNum(data.gaps) && live.gaps !== null && data.gaps !== live.gaps) {
          chart.stale = true;
        }
      }
      if (isNum(data.gaps)) {
        live.gaps = data.gaps;
      }
      if (bars.length) {
        applyList(live.tf, bars, 'stream');
      } else if (chart.stale) {
        requestCandles();
      }
    }
    safe(renderLiveState);
    safe(renderChartNote);
  }

  /*
   * Whether the page has been hidden for longer than HIDDEN_CLOSE_MS. A browser allows about six
   * connections to one server over HTTP/1.1, and every tab keeps its tick stream open (signals are
   * announced from hidden tabs too): a tab hidden that long gives its chart stream back, makes no
   * tail requests, and opens the stream again when it shows. A short hide (switching tabs for a
   * moment, a page shown for an instant) changes nothing.
   */
  function awayLong() {
    return document.hidden && live.hiddenSince !== null && Date.now() - live.hiddenSince > HIDDEN_CLOSE_MS;
  }

  /* Open the chart stream for the timeframe on screen (one at a time; the old one is closed). */
  function openChartStream() {
    if (!live.started || state.leaving || !chart.series || typeof window.EventSource !== 'function' || awayLong()) {
      return;
    }
    const tf = chart.tf;
    if (live.source && live.tf === tf && live.source.readyState !== window.EventSource.CLOSED) {
      return;
    }
    closeChartStream();
    let source;
    try {
      source = new window.EventSource('/api/candles/stream?timeframe=' + encodeURIComponent(tf));
    } catch (error) {
      scheduleChartReopen();
      return;
    }
    live.source = source;
    live.tf = tf;
    live.openedAt = Date.now();
    source.addEventListener('candles', function (event) {
      safe(function () {
        onChartEvent(source, event.data);
      });
    });
    source.addEventListener('error', function () {
      if (live.source !== source) {
        return;
      }
      // Reconnecting by itself (the browser does after a drop) or given up (an error answer:
      // 401, 422, 503): the tick or tail requests draw meanwhile.
      safe(renderLiveState);
      if (source.readyState === window.EventSource.CLOSED) {
        scheduleChartReopen();
      }
    });
  }

  function closeChartStream() {
    if (live.timer !== null) {
      window.clearTimeout(live.timer);
      live.timer = null;
    }
    if (live.source) {
      live.source.close();
      live.source = null;
    }
  }

  /* After an error answer: a pause that grows (2, 5, 10, 30 s), then a snapshot (it finds a session that ended) and a new stream. */
  function scheduleChartReopen() {
    if (live.timer !== null || state.leaving) {
      return;
    }
    const pause = LIVE_PAUSES_MS[Math.min(live.pause, LIVE_PAUSES_MS.length - 1)];
    live.pause += 1;
    live.timer = window.setTimeout(async function () {
      live.timer = null;
      await refreshSnapshot(); // goes to /login when the session has ended
      openChartStream();
    }, pause);
  }

  /* Every second: a chart stream that is missing, given up or silent for long is opened again. */
  function checkChartStream() {
    if (!live.started || state.leaving || !chart.series || typeof window.EventSource !== 'function') {
      return;
    }
    if (awayLong()) {
      if (live.source) {
        closeChartStream(); // a tab hidden for a while holds no chart stream (see awayLong)
      }
      return;
    }
    const source = live.source;
    if (!source || source.readyState === window.EventSource.CLOSED || live.tf !== chart.tf) {
      if (live.timer === null) {
        openChartStream();
      }
      return;
    }
    const quiet = Date.now() - Math.max(live.lastWall, live.openedAt);
    if (source.readyState === window.EventSource.OPEN && quiet > LIVE_RESTART_MS && Date.now() - live.restartedAt > LIVE_RESTART_MS) {
      live.restartedAt = Date.now();
      closeChartStream();
      openChartStream();
    }
  }

  // ------------------------------------------------- the view: zoom, scrolling, «Шууд ханш руу»

  /*
   * What the user looks at, to keep it across a reload of the candles: following the newest
   * candle (its distance from the right edge), or a place in the history (the candle at the
   * left edge, by its time, and the width in candles).
   */
  function captureView() {
    if (!chart.api || !chart.bars.length) {
      return null;
    }
    const scale = chart.api.timeScale();
    if (!(scale.width() > 0)) {
      return null; // not laid out (a hidden page): there is no view to keep
    }
    const range = scale.getVisibleLogicalRange();
    const first = scale.timeToIndex(toChartTime(chart.bars[0].time), false);
    const last = scale.timeToIndex(toChartTime(chart.bars[chart.bars.length - 1].time), false);
    if (!range || !isNum(range.from) || !isNum(range.to) || !(range.to > range.from) || !isNum(first) || !isNum(last)) {
      return null;
    }
    if (range.to >= last) {
      return { following: true, position: scale.scrollPosition() };
    }
    const at = Math.max(0, Math.min(chart.bars.length - 1, Math.round(range.from) - first));
    return { following: false, time: chart.bars[at].time, offset: range.from - (first + at), width: range.to - range.from };
  }

  function restoreView(view) {
    const scale = chart.api.timeScale();
    if (view.following) {
      scale.scrollToPosition(view.position, false);
      return;
    }
    const index = scale.timeToIndex(toChartTime(view.time), true);
    if (isNum(index)) {
      scale.setVisibleLogicalRange({ from: index + view.offset, to: index + view.offset + view.width });
    }
  }

  /* «Шууд ханш руу» is shown while the newest candle is out of view. */
  function updateFollowButton() {
    const button = $('chart-follow');
    if (!button) {
      return;
    }
    let away = false;
    if (chart.api && chart.bars.length && chart.loadedTf === chart.tf) {
      const scale = chart.api.timeScale();
      const range = scale.getVisibleLogicalRange();
      const index = scale.timeToIndex(toChartTime(chart.bars[chart.bars.length - 1].time), false);
      away = Boolean(range) && isNum(index) && (index > range.to || index < range.from);
      const axis = chart.api.priceScale('right').width();
      if (away && isNum(axis) && axis > 0) {
        button.style.setProperty('--axis-width', Math.round(axis) + 'px'); // just left of the price axis
      }
      // While the newest candle is in view, a zoom (wheel, pinch) keeps the right edge where it is,
      // as on TradingView: zooming in never pushes the newest candle out and stops the following.
      // Scrolled into the history, a zoom centres on the pointer.
      if (chart.anchored !== !away) {
        chart.anchored = !away;
        scale.applyOptions({ rightBarStaysOnScroll: !away });
      }
    }
    show(button, away);
  }

  /* Back to the newest candle at the same zoom; from there the chart follows new candles again. */
  function onFollowClick() {
    if (chart.api) {
      chart.api.timeScale().scrollToRealTime();
    }
    safe(updateFollowButton);
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
          volume: kept ? kept.volume : null,
          closed: !kept || kept.closed !== false,
        };
      }
    }
    chart.hover = bar;
    renderLegend();
  }

  /* O H L C V and the change from the previous close: the bar under the crosshair, otherwise the last bar. */
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
      setText($('ohlc-v'), 'V ' + DASH);
      setText(change, '');
      return;
    }
    setText($('ohlc-o'), 'O ' + fmtPrice(bar.open));
    setText($('ohlc-h'), 'H ' + fmtPrice(bar.high));
    setText($('ohlc-l'), 'L ' + fmtPrice(bar.low));
    setText($('ohlc-c'), 'C ' + fmtPrice(bar.close));
    setText($('ohlc-v'), 'V ' + fmtVolume(bar.volume));
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
    safe(renderLiveState); // the chip names the source too
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
        const main = el('span', 'tf-main');
        const why = el('span', 'tf-why'); // why this source cannot show it, in words (renderTimeframes)
        why.hidden = true;
        main.append(el('span', 'tf-words', item[1]), why);
        choose.append(main, el('span', 'tf-code', item[0]));
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
      const reason = able ? '' : missingWords(tf);
      choose.title = reason;
      const why = row.querySelector('.tf-why');
      if (why) {
        setText(why, reason);
        why.hidden = able;
      }
      star.setAttribute('aria-pressed', starred ? 'true' : 'false');
      star.setAttribute('aria-label', name + (starred ? ': дуртайгаас хасах' : ': дуртайд нэмэх'));
      star.title = starred ? 'Дуртайгаас хасах' : 'Дуртайд нэмэх';
    }
  }

  /* Why this source cannot show ``tf``: the server's reason code (snapshot.chart.missing) in words. */
  function missingWords(tf) {
    const words = pick(MISSING_WORDS, pick(chart.missing, tf));
    if (words) {
      return words(chart.shortest || state.timeframe);
    }
    return 'Энэ эх үүсвэр уг хугацааг өгдөггүй';
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
    const info = state.snap && state.snap.chart;
    const list = info && info.timeframes;
    if (!Array.isArray(list)) {
      return;
    }
    const next = list.filter(knownTf);
    const missing = {};
    if (info.missing && typeof info.missing === 'object') {
      for (const tf of TF_ORDER) {
        if (typeof pick(info.missing, tf) === 'string') {
          missing[tf] = info.missing[tf];
        }
      }
    }
    const shortest = knownTf(info.shortest) ? info.shortest : '';
    const key = JSON.stringify([next, missing, shortest]);
    if (chart.supported !== null && key === chart.supportKey && chart.supported.join(',') === next.join(',')) {
      return;
    }
    chart.supportKey = key;
    chart.supported = next;
    chart.missing = missing;
    chart.shortest = shortest;
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
    // A zoom or a scroll changes how far the forecast's points are from the TP and SL labels.
    chart.api.timeScale().subscribeVisibleLogicalRangeChange(function () {
      safe(placeForecastWord);
    });
    chart.api.timeScale().subscribeSizeChange(function () {
      safe(placeForecastWord);
    });
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
    if (
      skill.better_than_baseline &&
      !(
        skill.mae < skill.baseline_mae &&
        skill.calls >= FORECAST_MIN_CALLS &&
        skill.hit_rate > 0.5 &&
        hitRateFloor(Math.round(skill.hit_rate * skill.calls), skill.calls) > 0.5
      )
    ) {
      return null; // it claims an edge that its own numbers do not show
    }
    return skill;
  }

  /*
   * Lower 95% bound (Wilson) of a hit rate, the same sum as core/forecast.hit_rate_floor: calls one
   * candle apart overlap, so they count as calls / FORECAST_POINTS tries.
   */
  function hitRateFloor(hits, calls) {
    const tries = calls / FORECAST_POINTS;
    if (!(tries > 0)) {
      return 0;
    }
    const rate = hits / calls;
    const z = FORECAST_Z95;
    const centre = rate + (z * z) / (2 * tries);
    const spread = z * Math.sqrt((rate * (1 - rate)) / tries + (z * z) / (4 * tries * tries));
    return (centre - spread) / (1 + (z * z) / tries);
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
    chart.forecastShown = null;
    chart.wordKey = '';
    chart.forecast.label.setMarkers([]);
    chart.forecast.line.setData([]);
    chart.forecast.low.setData([]);
    chart.forecast.high.setData([]);
  }

  /*
   * Where the word «Таамаг» goes: on one of the forecast's points, above or below it. The TP and
   * SL labels sit at the right edge of the chart, and so does the end of the forecast, so the word
   * at the end could be half covered by one of them (seen with a TP near the last point). Sizes in
   * CSS pixels for the chart's 11 px font, measured on the page, with a margin: the word is about
   * 37 wide and lies 10 to 22 from its point; a TP / SL box is about 23 wide and 15 tall at the
   * chart's right edge. The chart moves a label by up to its own height, away from the other
   * one, when it would overlap another label (the live price, or the other level), so such a
   * label is taken to reach that much further on that side.
   */
  const WORD_HALF_WIDTH = 22;
  const WORD_NEAR = 8;
  const WORD_FAR = 24;
  const LEVEL_BOX_WIDTH = 28;
  const LEVEL_BOX_HEIGHT = 16;

  /* The boxes the TP and SL labels can take on the chart, in CSS pixels; [] when they are not drawn. */
  function levelBoxes() {
    const levels = chart.levels;
    if (!levels || !chart.api || !chart.series) {
      return [];
    }
    const width = chart.api.timeScale().width();
    const last = chart.bars[chart.bars.length - 1];
    const ys = [levels.min, levels.max].map(function (price) {
      return chart.series.priceToCoordinate(price);
    });
    const others = ys.concat(last ? [chart.series.priceToCoordinate(last.close)] : []);
    if (!isNum(width) || !ys.every(isNum)) {
      return null; // not laid out yet: no pixel answer
    }
    return ys.map(function (y, i) {
      const box = { left: width - LEVEL_BOX_WIDTH, top: y - LEVEL_BOX_HEIGHT / 2, bottom: y + LEVEL_BOX_HEIGHT / 2 };
      others.forEach(function (other, j) {
        if (j === i || !isNum(other) || Math.abs(other - y) >= LEVEL_BOX_HEIGHT) {
          return;
        }
        // Crowded: the label is moved away from the other one (either way when they are level).
        if (other <= y) {
          box.bottom += LEVEL_BOX_HEIGHT;
        }
        if (other >= y) {
          box.top -= LEVEL_BOX_HEIGHT;
        }
      });
      return box;
    });
  }

  /*
   * The first place where the word is clear of every TP / SL box: the last point, then the ones
   * before it, each first on the side the line does not come from, then the line's start (the
   * close it was made from), first on the side away from the signal's arrows. Without a signal
   * (no TP / SL on the chart): the last point. When no place is clear, or the
   * chart cannot say where things are, the last point on the side with more room before the next
   * TP or SL line (in price).
   */
  function forecastWordPlace(p) {
    const points = p.points;
    const end = points[points.length - 1];
    const down = end.close < p.base_price;
    const last = { time: end.time, side: down ? 'belowBar' : 'aboveBar', price: end.close };
    if (!chart.levels) {
      return last;
    }
    const boxes = levelBoxes();
    if (boxes) {
      const places = points
        .map(function (point) {
          return { time: point.time, price: point.close, first: last.side };
        })
        .reverse();
      // At the start the word shares its candle with that candle's signal arrow, if it has one:
      // SELL arrows stand above the candles, BUY arrows below.
      const card = state.tick && state.tick.card;
      const arrows = card && card.kind === 'signal' ? card.side : null;
      const start = arrows === 'SELL' ? 'belowBar' : arrows === 'BUY' ? 'aboveBar' : down ? 'aboveBar' : 'belowBar';
      places.push({ time: p.made_at - STEP[FORECAST_TF], price: p.base_price, first: start });
      for (const place of places) {
        const x = chart.api.timeScale().timeToCoordinate(toChartTime(place.time));
        const y = chart.series.priceToCoordinate(place.price);
        if (!isNum(x) || !isNum(y)) {
          continue;
        }
        for (const side of place.first === 'aboveBar' ? ['aboveBar', 'belowBar'] : ['belowBar', 'aboveBar']) {
          const top = side === 'aboveBar' ? y - WORD_FAR : y + WORD_NEAR;
          const bottom = side === 'aboveBar' ? y - WORD_NEAR : y + WORD_FAR;
          const covered = boxes.some(function (box) {
            return x + WORD_HALF_WIDTH > box.left && bottom > box.top && top < box.bottom;
          });
          if (!covered) {
            return { time: place.time, side: side };
          }
        }
      }
    }
    let above = Infinity;
    let below = Infinity;
    for (const level of [chart.levels.min, chart.levels.max]) {
      if (level > end.close) {
        above = Math.min(above, level - end.close);
      } else if (level < end.close) {
        below = Math.min(below, end.close - level);
      }
    }
    return { time: end.time, side: above === below ? last.side : above > below ? 'aboveBar' : 'belowBar' };
  }

  /* Put the word where forecastWordPlace says; again after a zoom, a scroll or a new tick. */
  function placeForecastWord() {
    const shown = chart.forecastShown;
    if (!chart.forecast || !shown) {
      return;
    }
    const place = forecastWordPlace(shown.p);
    const key = [chart.forecastKey, place.time, place.side].join('|');
    if (key === chart.wordKey) {
      return;
    }
    chart.wordKey = key;
    chart.forecast.label.setMarkers([
      {
        time: toChartTime(place.time),
        position: place.side,
        shape: 'circle',
        size: 0.5,
        color: shown.dim ? FORECAST_COLOR.lineDim : FORECAST_COLOR.line,
        text: 'Таамаг',
      },
    ]);
  }

  function drawForecastSeries(p, dim) {
    chart.forecastShown = { p: p, dim: dim };
    const parts = [chart.shift, dim ? 1 : 0, p.made_at, p.base_price];
    for (const point of p.points) {
      parts.push(point.time, point.close, point.low, point.high);
    }
    const key = parts.join('|');
    if (key === chart.forecastKey) {
      placeForecastWord(); // the price scale or the TP / SL lines may have moved
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
    chart.forecastKey = key;
    placeForecastWord();
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
    chart.rev = null;
    chart.retryAt = 0;
    chart.unavailable = null;
    live.gaps = null;
    live.fallbackWall = 0;
    live.fallbackAt = null;
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
      safe(function () {
        if (chart.volume) {
          chart.volume.setData([]);
        }
      });
      if (chart.markers) {
        chart.markers.setMarkers([]);
      }
      renderLegend();
      safe(updatePriceLabel);
      safe(updateFollowButton);
      chartMessage('График ачаалж байна');
      loadCandles(true);
      // The chart stream of the new timeframe; the old one's events are no longer drawn.
      live.pause = 0;
      openChartStream();
    }
    safe(renderLiveState);
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
    closeChartStream();
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
    $('chart-follow').addEventListener('click', onFollowClick);

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
        if (live.hiddenSince === null) {
          live.hiddenSince = Date.now(); // the chart stream is given back if it stays hidden (awayLong)
        }
        return;
      }
      live.hiddenSince = null;
      if (document.hasFocus()) {
        clearTitle();
      }
      requestCandles(); // the page was away: load the candles again
      if (state.tick && state.tick.v !== state.snapV) {
        refreshSnapshot();
      }
      checkStream();
      safe(checkChartStream);
      safe(renderNotifyButton);
    });
    window.addEventListener('online', function () {
      checkStream();
      safe(checkChartStream);
    });
    window.addEventListener('pagehide', function (event) {
      if (!event.persisted) {
        closeStream();
        closeChartStream();
      }
    });
    window.addEventListener('pageshow', function (event) {
      if (event.persisted && !state.leaving) {
        stream.broken = true;
        checkStream();
        safe(checkChartStream);
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
    live.started = true;
    openChartStream(); // and the chart's own candle stream for the timeframe on screen
  }

  boot();
})();
