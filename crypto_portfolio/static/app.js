/* Crypto Portfolio PWA - Vue 3 (global build) + TradingView Lightweight Charts v5.
 *
 * Talks only to this server's /api. Data flow:
 *   GET  /api/portfolio           instant: last live fetch or newest SQLite snapshot
 *   POST /api/portfolio/refresh   live fetch from Binance + chains, records a snapshot
 *   GET  /api/portfolio/history   snapshot totals for the chart
 *   GET  /api/portfolio/attribution  why the total moved: price vs quantity effect per coin
 *   GET  /api/analytics/quant     Analytics: TWR, base 100 vs BTC/ETH, max drawdown, volatility
 *
 * Two sections behind a bottom tab bar: Portfolio (glance: total, chart, assets) and Analytics
 * (attribution, performance, risk), both driven by one period selector. Sections are kept
 * mounted (v-show) so charts survive a switch; they are re-fitted when their section shows.
 *   GET/POST /api/settings (+ test-binance, test-wallet) for the settings sheet
 */
(function () {
  'use strict';

  const { createApp, reactive, ref, computed, watch, nextTick, onMounted, onBeforeUnmount } = Vue;
  const LWC = window.LightweightCharts;

  // -- constants ---------------------------------------------------------------
  const RANGES = [
    // `interval`: one point per bucket of that length (services/resample.py). Lightweight Charts
    // spaces points by index, so a regular cadence is what keeps the X axis proportional to time.
    { id: '24H', days: 1, interval: '1h', label: 'Past 24 hours' },
    { id: '7D', days: 7, interval: '4h', label: 'Past 7 days' },
    { id: '1M', days: 30, interval: '1d', label: 'Past month' },
    { id: '1Y', days: 365, interval: '1w', label: 'Past year' },
    { id: '5Y', days: 1827, interval: '1mo', label: 'Past 5 years' },
    { id: 'ALL', days: 0, interval: '1mo', label: 'All time' },
  ];
  const TABS = [
    { id: 'all', label: 'All', hint: 'Every source' },
    { id: 'binance', label: 'Binance', hint: 'Spot + Simple Earn + Funding' },
    { id: 'onchain', label: 'On-chain', hint: 'Every configured wallet and chain' },
  ];
  const SOURCE_ORDER = ['binance_spot', 'binance_earn', 'binance_funding', 'onchain'];
  const BINANCE_SOURCES = ['binance_spot', 'binance_earn', 'binance_funding'];
  const CHAIN_COLORS = {
    ethereum: '#7B93F0', bsc: '#F0B90B', arbitrum: '#28A0F0',
    base: '#3D7BFF', polygon: '#9B6CF0', solana: '#14F195',
  };
  // Logos shipped in /static/coins (CC0, spothq/cryptocurrency-icons). Anything else gets a badge.
  const COIN_ICONS = new Set((
    '1inch aave ada algo ape atom avax band bat bch bnb btc chz comp crv dai dash doge dot enj eos ' +
    'etc eth fil grt icp icx ksm link ltc mana matic mkr neo omg ont pax pol qtum rvn sand snx sol ' +
    'storj sushi theta trx tusd uni usdc usdt vet waves wbtc xlm xmr xrp xtz yfi zec zil zrx'
  ).split(' '));
  const COLORS = { up: '#22E58B', down: '#FF5C73', flat: '#A3ADBA' };
  const STALE_MINUTES = 10;   // older than this on open or on return -> refresh in the background
  const EVM_RE = /^0x[0-9a-fA-F]{40}$/;
  const SOLANA_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
  const BINANCE_KEY_LENGTH = 64;   // HMAC API keys and secrets are both 64 letters and digits
  const SCRUB_HOLD_MS = 130;       // a finger resting this long on the chart starts scrubbing
  const SCRUB_SLOP_PX = 8;         // ...or one that moved this far sideways first
  const STORE = { privacy: 'portfolio.privacy', range: 'portfolio.range', tab: 'portfolio.tab',
    period: 'portfolio.analytics.period' };
  const SECTIONS = ['portfolio', 'analytics'];
  // One period drives the whole Analytics section. `why` is the attribution window
  // (GET /api/portfolio/attribution, at most 365d); `quant` the period of
  // GET /api/analytics/quant, which works on daily closes and so needs two of them at least.
  const ANALYTICS_PERIODS = [
    { id: '24H', why: '24h', quant: '2d', quantNote: 'Performance and risk use daily closes, so the shortest view is the last 2 days.' },
    { id: '7D', why: '7d', quant: '7d' },
    { id: '30D', why: '30d', quant: '30d' },
    { id: '1Y', why: '365d', quant: '1y' },
    { id: 'ALL', why: '365d', quant: 'all', whyNote: 'The breakdown looks back one year at most.' },
  ];
  const WHY_TOP = 4;   // coins listed on each side (pulling up / pulling down)
  const PEEK_TOP = 3;  // coins in the 24h peek sheet
  const BENCH_COLORS = { portfolio: '#22E58B', BTC: '#F7931A', ETH: '#8AB4FF' };

  // -- formatting --------------------------------------------------------------
  const usd2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const usd0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  const usdCompact = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 2 });
  const usdSig = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumSignificantDigits: 4 });
  const qtyFormats = {
    big: new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }),
    mid: new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 }),
    small: new Intl.NumberFormat('en-US', { maximumSignificantDigits: 4 }),
  };

  const num = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);

  const fmt = {
    money: (value) => usd2.format(num(value)),
    compactMoney(value) {
      const v = num(value);
      return Math.abs(v) < 100000 ? usd0.format(v) : usdCompact.format(v);
    },
    price(value) {
      const v = num(value);
      if (!v) return 'No price';
      return v >= 1 ? usd2.format(v) : usdSig.format(v);
    },
    qty(value) {
      const v = num(value);
      if (v === 0) return '0';
      const a = Math.abs(v);
      return (a >= 1000 ? qtyFormats.big : a >= 1 ? qtyFormats.mid : qtyFormats.small).format(v);
    },
    pct(value) {
      const v = num(value);
      if (v > 0 && v < 0.01) return '<0.01%';
      return v.toFixed(v >= 10 ? 1 : 2) + '%';
    },
    signedMoney: (value) => (num(value) < 0 ? '−' : '+') + usd2.format(Math.abs(num(value))),
    signedPct: (value) => (num(value) < 0 ? '−' : '+') + Math.abs(num(value)).toFixed(2) + '%',
    hue(symbol) {
      let h = 0;
      for (const ch of String(symbol)) h = (h * 31 + ch.charCodeAt(0)) % 360;
      return h;
    },
    badge(symbol) {
      const clean = String(symbol).replace(/[^A-Z0-9]/gi, '');
      return (clean || String(symbol)).slice(0, 3).toUpperCase();
    },
    shortAddress(address, head = 6) {
      if (!address) return '';
      return address.length <= head + 6 ? address : address.slice(0, head) + '…' + address.slice(-4);
    },
    fullDate(iso) {
      const d = new Date(iso);
      return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
    },
    when(seconds) {
      const d = new Date(seconds * 1000);
      const opts = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' };
      if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
      return d.toLocaleString(undefined, opts);
    },
    day(seconds) {
      const d = new Date(seconds * 1000);
      const opts = { month: 'short', day: 'numeric' };
      if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
      return d.toLocaleDateString(undefined, opts);
    },
    ago(iso, nowMs) {
      const t = Date.parse(iso);
      if (Number.isNaN(t)) return 'never';
      const s = Math.max(0, (nowMs - t) / 1000);
      if (s < 60) return 'just now';
      if (s < 3600) return Math.floor(s / 60) + ' min ago';
      if (s < 86400) return Math.floor(s / 3600) + ' h ago';
      return Math.floor(s / 86400) + ' d ago';
    },
    // Label of the hero chip. "24h" only when the reference really is about a day old; a hole in
    // the history or a young one shows the span actually covered.
    hours(h, requested = 24) {
      if (Math.abs(h - requested) < requested * 0.15) return requested + 'h';
      if (h >= 48) return 'past ' + (h / 24).toFixed(h < 240 ? 1 : 0) + ' days';
      if (h >= 1) return 'past ' + Math.round(h) + 'h';
      return 'past ' + Math.max(1, Math.round(h * 60)) + ' min';
    },
  };

  const trendOf = (value) => (value > 0.004 ? 'up' : value < -0.004 ? 'down' : 'flat');

  const hexToRgba = (hex, alpha) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  };

  const storage = {
    get(key, fallback) {
      try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch (_) { /* private mode: preference just won't stick */ }
    },
  };

  // -- API ---------------------------------------------------------------------
  class ApiError extends Error {
    constructor(status, message, detail) {
      super(message);
      this.status = status;
      this.detail = detail;
    }
  }

  function errorMessage(data, status) {
    const detail = data && data.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map((e) => {
        const where = (e.loc || []).filter((p) => p !== 'body').join(' › ');
        return where ? `${where}: ${e.msg}` : e.msg;
      }).join('; ');
    }
    if (detail && typeof detail === 'object' && detail.message) return detail.message;
    if (status === 0) return 'The server is unreachable. Is `main.py serve` still running?';
    return `Request failed (HTTP ${status}).`;
  }

  async function api(path, { method = 'GET', body } = {}) {
    let response;
    try {
      response = await fetch(path, {
        method,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (_) {
      throw new ApiError(0, errorMessage(null, 0), null);
    }
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) throw new ApiError(response.status, errorMessage(data, response.status), data && data.detail);
    return data;
  }

  const emptyPortfolio = () => ({
    timestamp: '', total_usd: 0, origin: 'empty', configured: true, assets: [], errors: [],
    groups: { all: 0, binance: 0, onchain: 0 }, source_labels: {}, change_24h: null,
    dust: { assets: 0, value_usd: 0 }, min_value: 1,
  });

  // -- app ---------------------------------------------------------------------
  const app = createApp({
    setup() {
      // portfolio state
      const loading = ref(true);
      const refreshing = ref(false);
      const portfolio = ref(emptyPortfolio());
      const points = ref([]);
      const historyLoaded = ref(false);
      const rangeId = ref(RANGES.some((r) => r.id === storage.get(STORE.range)) ? storage.get(STORE.range) : '1M');
      const tabId = ref(TABS.some((t) => t.id === storage.get(STORE.tab)) ? storage.get(STORE.tab) : 'all');
      const privacy = ref(storage.get(STORE.privacy, '0') === '1');
      const scrub = ref(null);
      const expanded = ref(null);
      const showErrors = ref(false);
      const iconFailed = reactive({});
      const toast = ref(null);
      const scrolled = ref(false);
      const nowMs = ref(Date.now());

      // sections: the app always opens on Portfolio (a one-second glance)
      const activeTab = ref('portfolio');
      const tabScroll = { portfolio: 0, analytics: 0 };   // each section keeps its own scroll position

      // Analytics: one period for attribution, performance and risk
      const periodId = ref(ANALYTICS_PERIODS.some((p) => p.id === storage.get(STORE.period)) ? storage.get(STORE.period) : '30D');
      const analyticsPeriod = computed(() => ANALYTICS_PERIODS.find((p) => p.id === periodId.value) || ANALYTICS_PERIODS[2]);
      const why = reactive({ data: null, loading: false, error: '' });
      let whySeq = 0;
      const insights = reactive({ data: null, loading: false, error: '' });
      const benchEl = ref(null);
      const underwaterEl = ref(null);
      let insightSeq = 0;
      let benchChart = null;
      let underwaterChart = null;
      let insightChartsStale = false;   // data arrived while Analytics was hidden: draw on show

      // "Why it moved" peek sheet, opened from the 24h chip
      const peek = reactive({ open: false, data: null, loading: false, error: '' });
      const peekSheet = ref(null);
      const peekClose = ref(null);
      let peekSeq = 0;

      // access token
      const locked = ref(false);
      const unlockToken = ref('');
      const unlockError = ref('');
      const unlocking = ref(false);

      // settings state
      const settingsOpen = ref(false);
      const settings = ref(null);
      const form = reactive({ binance: { enabled: true, api_key: '', api_secret: '', clear: false }, wallets: [] });
      const baseline = ref('');
      const showKey = ref(false);
      const showSecret = ref(false);
      const saving = ref(false);
      const saveError = ref('');
      const binanceTest = reactive({ state: 'idle', result: null });
      const draft = ref(null);
      const draftError = ref('');
      const armedDelete = ref(-1);

      // template refs
      const chartWrap = ref(null);
      const chartEl = ref(null);
      const sheet = ref(null);
      const sheetClose = ref(null);
      const draftEl = ref(null);

      let chart = null;
      let series = null;
      let touchSurface = null;
      const historyCache = new Map();
      let toastTimer = null;
      let armTimer = null;
      let tickTimer = null;
      let toastSeq = 0;
      let lastFocus = null;

      // -- helpers -------------------------------------------------------------
      function showToast(text, kind = 'ok', ms = 3200) {
        clearTimeout(toastTimer);
        toast.value = { id: ++toastSeq, text, kind };
        toastTimer = setTimeout(() => { toast.value = null; }, ms);
      }

      function handleError(error, { silent = false } = {}) {
        if (error && error.status === 401) {
          locked.value = true;
          return;
        }
        if (!silent) showToast(error.message || String(error), 'error', 5000);
      }

      const ageMinutes = () => {
        const t = Date.parse(portfolio.value.timestamp);
        return Number.isNaN(t) ? Infinity : (Date.now() - t) / 60000;
      };

      function normalise(data) {
        const base = emptyPortfolio();
        return {
          ...base,
          ...data,
          assets: Array.isArray(data.assets) ? data.assets : [],
          errors: Array.isArray(data.errors) ? data.errors : [],
          groups: { ...base.groups, ...(data.groups || {}) },
          dust: data.dust || base.dust,
        };
      }

      // -- computed: hero ------------------------------------------------------
      const range = computed(() => RANGES.find((r) => r.id === rangeId.value) || RANGES[2]);
      const rangeLabel = computed(() => range.value.label);

      const heroParts = computed(() => {
        const value = scrub.value ? scrub.value.value : portfolio.value.total_usd;
        const text = fmt.money(value);
        const dot = text.lastIndexOf('.');
        return dot === -1 ? { whole: text, cents: '', chars: text.length }
          : { whole: text.slice(0, dot), cents: text.slice(dot), chars: text.length };
      });

      const rangeChange = computed(() => {
        const pts = points.value;
        if (pts.length < 2) return null;
        const first = pts[0].total_usd;
        const usd = pts[pts.length - 1].total_usd - first;
        return { usd, pct: first > 0 ? (usd / first) * 100 : null, trend: trendOf(usd) };
      });

      const heroChange = computed(() => {
        if (scrub.value) {
          const usd = scrub.value.value - scrub.value.first;
          return {
            usd, pct: scrub.value.first > 0 ? (usd / scrub.value.first) * 100 : null,
            trend: trendOf(usd), label: 'since ' + scrub.value.firstDay,
          };
        }
        const change = portfolio.value.change_24h;
        if (!change) return null;
        return {
          usd: change.usd, pct: change.pct, trend: trendOf(change.usd), label: fmt.hours(change.hours, change.requested_hours || 24),
          explainable: !!change.has_attribution,  // tapping the chip opens the "Why it moved" peek
        };
      });

      const heroTrend = computed(() => (heroChange.value ? heroChange.value.trend : 'flat'));

      const scrubLabel = computed(() => (scrub.value ? scrub.value.when : ''));

      const chartMessage = computed(() => {
        if (loading.value || !historyLoaded.value) return '';
        const n = points.value.length;
        if (n >= 2) return '';
        if (!portfolio.value.configured) return 'Your history chart starts with the first refresh.';
        if (n === 1) return 'One snapshot so far. The line appears from the second one.';
        if (range.value.days) return `No snapshots in the ${range.value.label.toLowerCase()}. Each refresh, and each cron \`record\`, adds one.`;
        return 'No snapshots yet. Each refresh, and each cron `record`, adds one.';
      });

      const updatedAgo = computed(() => fmt.ago(portfolio.value.timestamp, nowMs.value));

      // -- computed: attribution ---------------------------------------------------
      // What made the total move: market price vs holdings change, per coin (§2 of ROADMAP_LEDGER).
      // The same helpers feed the Analytics section and the 24h peek sheet.
      const attributionReady = (data) => !!(data && !data.insufficient_history);

      function attributionBuckets(data) {
        const b = data.buckets || {};
        const other = num(b.interaction) + num(b.revaluation);
        return [
          { id: 'price', label: 'Market prices', value: num(b.price), hint: 'Same holdings, new prices' },
          { id: 'quantity', label: 'Holdings change', value: num(b.quantity), hint: 'Deposits, withdrawals, trades, Earn interest' },
          { id: 'other', label: 'Other', value: other, hint: 'Price × quantity overlap and newly (un)priced tokens', optional: true },
          { id: 'data_gap', label: 'Missing data', value: num(b.data_gap), hint: 'A source failed in one of the snapshots', optional: true },
        ].filter((bucket) => !bucket.optional || Math.abs(bucket.value) >= 0.005)
          .map((bucket) => ({ ...bucket, trend: trendOf(bucket.value) }));
      }

      // Bars are scaled to the biggest mover so the list reads as relative weight.
      function attributionRows(rows) {
        const scale = Math.max(...rows.map((r) => Math.abs(num(r.total))), 0.01);
        return rows.map((r) => {
          const total = num(r.total);
          const price = num(r.price);
          const quantity = num(r.quantity);
          const byPrice = Math.abs(price) >= Math.abs(quantity);
          const pct = r.price_change_pct;
          let note;
          if (Math.abs(num(r.data_gap)) > Math.abs(total) / 2) note = 'missing data';
          else if (byPrice) note = pct === null || pct === undefined ? 'price' : 'price ' + fmt.signedPct(pct);
          else note = quantity > 0 ? 'more held' : 'less held';
          const parts = [['Price', price], ['Holdings', quantity], ['Overlap', num(r.interaction)],
            ['Revaluation', num(r.revaluation)], ['Missing data', num(r.data_gap)]]
            .filter(([, v]) => Math.abs(v) >= 0.005)
            .map(([label, v]) => `${label} ${fmt.signedMoney(v)}`);
          return {
            key: r.key, total, trend: trendOf(total), note, byPrice,
            width: Math.max(2, (Math.abs(total) / scale) * 50),
            breakdown: parts.join(' · '),
          };
        });
      }

      // Real span covered (the window can be shorter than asked when the history is).
      function attributionSpan(data) {
        const h = num(data.hours);
        if (h < 1) return Math.max(1, Math.round(h * 60)) + ' min';
        return h < 48 ? Math.round(h) + 'h' : (h / 24).toFixed(h < 240 ? 1 : 0) + ' days';
      }

      // Why the span differs from the period asked for; '' when it matches (coverage "full").
      // "partial" starting at the very first snapshot means tracking began inside the window;
      // anything else is a hole in the history, so the closest snapshots were used instead.
      function attributionSpanNote(data) {
        if (!data.coverage || data.coverage === 'full') return '';
        if (data.coverage === 'partial' && data.history_start && data.history_start === data.from) {
          const started = Date.parse(data.from);
          if (!Number.isNaN(started)) return 'tracking started ' + fmt.day(started / 1000);
        }
        const asked = num(data.requested_hours);
        if (!asked) return '';
        const period = asked < 48 ? asked + 'h' : Math.round(asked / 24) + ' days';
        return 'closest data to ' + period;
      }
      function attributionSpanFull(data) {
        const note = attributionSpanNote(data);
        return attributionSpan(data) + (note ? ` (${note})` : '');
      }

      const INSUFFICIENT_WHY = 'Needs at least two snapshots in this window. Each refresh, and each cron `record`, adds one.';

      const whyReady = computed(() => attributionReady(why.data));
      const whyBuckets = computed(() => (whyReady.value ? attributionBuckets(why.data) : []));
      const whyRows = computed(() => (whyReady.value
        ? attributionRows([...(why.data.gainers || []), ...(why.data.losers || [])]) : []));
      const whyTrend = computed(() => (whyReady.value ? trendOf(num(why.data.delta_usd)) : 'flat'));
      const whySpan = computed(() => (whyReady.value ? attributionSpanFull(why.data) : ''));
      const whyOthers = computed(() => (whyReady.value && why.data.others && why.data.others.count ? why.data.others : null));
      const whyMessage = computed(() => {
        if (why.error) return why.error;
        if (why.data && why.data.insufficient_history) return INSUFFICIENT_WHY;
        if (whyReady.value && !whyRows.value.length) return 'Nothing moved by more than a cent.';
        return '';
      });

      // Peek: the 24h move in one look - total, price vs holdings, the three biggest movers.
      const peekReady = computed(() => attributionReady(peek.data));
      const peekTrend = computed(() => (peekReady.value ? trendOf(num(peek.data.delta_usd)) : 'flat'));
      const peekSpan = computed(() => (peekReady.value ? attributionSpan(peek.data) : ''));
      const peekSpanNote = computed(() => (peekReady.value ? attributionSpanNote(peek.data) : ''));
      const peekBuckets = computed(() => (peekReady.value
        ? attributionBuckets(peek.data).filter((b) => b.id === 'price' || b.id === 'quantity') : []));
      const peekRows = computed(() => {
        if (!peekReady.value) return [];
        const rows = [...(peek.data.gainers || []), ...(peek.data.losers || [])]
          .sort((a, b) => Math.abs(num(b.total)) - Math.abs(num(a.total)))
          .slice(0, PEEK_TOP);
        return attributionRows(rows);
      });
      const peekMessage = computed(() => {
        if (peek.error) return peek.error;
        if (peek.data && peek.data.insufficient_history) return INSUFFICIENT_WHY;
        if (peekReady.value && !peekRows.value.length) return 'Nothing moved by more than a cent.';
        return '';
      });

      // -- computed: assets ----------------------------------------------------
      const visibleAssets = computed(() => {
        const p = portfolio.value;
        const tab = tabId.value;
        const groupTotal = num(p.groups[tab]);
        const labels = p.source_labels || {};
        const minValue = num(p.min_value);
        const sources = tab === 'binance' ? BINANCE_SOURCES : tab === 'onchain' ? ['onchain'] : SOURCE_ORDER;

        return p.assets
          .map((asset) => {
            let quantity = num(asset.quantity);
            let value = num(asset.value_usd);
            let pct = num(asset.pct);
            if (tab === 'binance') {
              quantity = num(asset.binance_quantity);
              value = num(asset.binance_value_usd);
              pct = groupTotal > 0 ? (value / groupTotal) * 100 : 0;
            } else if (tab === 'onchain') {
              quantity = num(asset.onchain_quantity);
              value = num(asset.onchain_value_usd);
              pct = groupTotal > 0 ? (value / groupTotal) * 100 : 0;
            }
            const breakdown = sources
              .filter((s) => num((asset.by_source || {})[s]) > 0)
              .map((s) => ({
                source: s,
                label: labels[s] || s,
                quantity: num(asset.by_source[s]),
                value: num((asset.value_by_source || {})[s]),
              }));
            return {
              symbol: asset.symbol, quantity, value, pct, price: num(asset.price_usd), breakdown,
              locations: tab === 'binance' ? (asset.locations || []).filter((l) => !l.includes('/'))
                : tab === 'onchain' ? (asset.locations || []).filter((l) => l.includes('/'))
                  : (asset.locations || []),
            };
          })
          .filter((a) => a.quantity > 0 && (tab === 'all' || a.value >= minValue))
          .sort((a, b) => b.value - a.value || a.symbol.localeCompare(b.symbol));
      });

      // Width of the value column: wide enough for the longest balance or price in the list, so
      // money is never cut off. Tabular Manrope digits are ~9.3px at 15px/800, ~7.8px at 13px/600.
      const valueCol = computed(() => {
        let width = 104;
        for (const a of visibleAssets.value) {
          width = Math.max(width, fmt.money(a.value).length * 9.3, fmt.price(a.price).length * 7.8);
        }
        return Math.ceil(width) + 'px';
      });

      const emptyTitle = computed(() => {
        if (portfolio.value.origin === 'empty') return 'No balances fetched yet';
        if (tabId.value === 'binance') return 'No Binance balances to show';
        if (tabId.value === 'onchain') return 'No on-chain balances to show';
        return 'No balances above the dust filter';
      });
      const emptyHint = computed(() => {
        const min = fmt.money(portfolio.value.min_value);
        if (portfolio.value.origin === 'empty') return 'Fetch balances to take your first snapshot.';
        if (tabId.value === 'binance') return `Binance is not connected, or nothing there is worth ${min} or more.`;
        if (tabId.value === 'onchain') return `No wallet is configured, or nothing on-chain is worth ${min} or more.`;
        return `Every asset is worth less than ${min}. They still count in the total.`;
      });

      // -- data loading --------------------------------------------------------
      async function loadPortfolio() {
        portfolio.value = normalise(await api('/api/portfolio'));
      }

      async function loadHistory({ force = false } = {}) {
        const { days, interval } = range.value;
        if (force) historyCache.clear();
        try {
          if (!historyCache.has(days)) {
            const tz = -new Date().getTimezoneOffset();   // day/week/month edges follow the viewer's clock
            const data = await api(`/api/portfolio/history?days=${days}&interval=${interval}&tz_offset=${tz}`);
            historyCache.set(days, Array.isArray(data.points) ? data.points : []);
          }
          if (range.value.days === days) points.value = historyCache.get(days);
        } catch (error) {
          handleError(error, { silent: true });
          points.value = [];
        } finally {
          historyLoaded.value = true;
        }
        drawChart();
      }

      async function loadAttribution() {
        const seq = ++whySeq;  // a slower answer for a period the user already left is dropped
        why.loading = true;
        try {
          const data = await api(`/api/portfolio/attribution?window=${analyticsPeriod.value.why}&top=${WHY_TOP}`);
          if (seq !== whySeq) return;
          why.data = data;
          why.error = '';
        } catch (error) {
          if (seq !== whySeq) return;
          handleError(error, { silent: true });
          why.data = null;
          why.error = error.status === 401 ? '' : 'Could not explain the move: ' + error.message;
        } finally {
          if (seq === whySeq) why.loading = false;
        }
      }

      async function loadInsights() {
        const seq = ++insightSeq;
        insights.loading = true;
        try {
          const data = await api(`/api/analytics/quant?period=${analyticsPeriod.value.quant}`);
          if (seq !== insightSeq) return;
          insights.data = data;
          insights.error = '';
        } catch (error) {
          if (seq !== insightSeq) return;
          handleError(error, { silent: true });
          insights.data = null;
          insights.error = error.status === 401 ? '' : 'Could not load performance: ' + error.message;
        } finally {
          if (seq === insightSeq) insights.loading = false;
        }
        if (seq !== insightSeq) return;
        await nextTick();
        drawInsightCharts();
      }

      function loadAnalytics() {
        loadAttribution();
        loadInsights();
      }

      function setPeriod(id) {
        if (periodId.value === id) return;
        periodId.value = id;
        storage.set(STORE.period, id);
        loadAnalytics();
      }

      const insightsReady = computed(() => !!(insights.data && insights.data.available));
      const insightMessage = computed(() => {
        if (insights.error) return insights.error;
        if (insights.data && !insights.data.available) {
          return 'Performance needs daily closes on at least two days. Come back tomorrow.';
        }
        return '';
      });
      // One tile per metric; each carries its own "not enough data" state.
      const insightTiles = computed(() => {
        if (!insightsReady.value) return [];
        const d = insights.data;
        const vol = d.volatility || {};
        const dd = d.drawdown || {};
        const twr = d.twr || {};
        return [
          {
            id: 'twr', label: 'TWR', value: twr.twr_pct, signed: true,
            note: twr.annualized_pct !== undefined ? `${fmt.signedPct(twr.annualized_pct)} / year` : 'flows removed',
            hint: 'Time-weighted return: growth of $100 invested, with deposits and withdrawals taken out',
            sufficient: d.n_obs >= 1,
          },
          {
            id: 'mdd', label: 'Max drawdown', value: dd.max_drawdown_pct, signed: false, down: true,
            note: dd.peak ? (dd.recovered ? 'recovered' : 'not recovered') : 'no drawdown',
            hint: dd.peak ? (dd.recovered ? `Peak ${dd.peak}, recovered ${dd.recovery}` : `Peak ${dd.peak}, not recovered yet`)
              : 'Worst fall from a high-water mark of the TWR index',
            sufficient: d.n_obs >= 1,
          },
          {
            id: 'vol', label: 'Volatility', value: vol.annualized_pct, signed: false,
            note: vol.sufficient ? 'annualised' : `needs ${Math.max(0, d.min_observations - (vol.n_obs || 0))} more days`,
            hint: 'Standard deviation of daily returns × √365',
            sufficient: !!vol.sufficient && vol.annualized_pct !== null,
          },
        ].map((t) => ({
          ...t,
          text: t.value === null || t.value === undefined ? '—' : (t.signed ? fmt.signedPct(t.value) : num(t.value).toFixed(2) + '%'),
          trend: t.value === null || t.value === undefined ? 'flat' : (t.down ? (num(t.value) < 0 ? 'down' : 'flat') : trendOf(num(t.value))),
        }));
      });

      // Legend of the base-100 chart doubles as the scoreboard: each line's return over the period.
      const benchLegend = computed(() => {
        if (!insightsReady.value) return [];
        const d = insights.data;
        const twr = d.twr || {};
        const rows = [{ key: 'portfolio', label: 'You', pct: twr.twr_pct }];
        for (const symbol of ['BTC', 'ETH']) {
          const b = (d.benchmarks || {})[symbol];
          if (b) rows.push({ key: symbol, label: symbol, pct: b.available ? b.return_pct : null });
        }
        return rows.map((r) => ({
          ...r, color: BENCH_COLORS[r.key],
          text: r.pct === null || r.pct === undefined ? 'no data' : fmt.signedPct(r.pct),
        }));
      });

      function insightChartOptions() {
        return {
          autoSize: true,
          layout: {
            background: { type: LWC.ColorType.Solid, color: 'transparent' },
            textColor: '#8590A0', fontFamily: 'Manrope, system-ui, sans-serif', fontSize: 11,
            attributionLogo: false,
          },
          grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(233, 237, 242, 0.05)' } },
          rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
          timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
          crosshair: { mode: LWC.CrosshairMode.Magnet },
          handleScroll: false,
          handleScale: false,
        };
      }

      function destroyInsightCharts() {
        if (benchChart) benchChart.remove();
        if (underwaterChart) underwaterChart.remove();
        benchChart = null;
        underwaterChart = null;
      }

      function drawInsightCharts() {
        // A chart created inside a hidden (display: none) section starts at zero width; wait
        // until Analytics is on screen and draw it then.
        if (activeTab.value !== 'analytics') { insightChartsStale = true; return; }
        insightChartsStale = false;
        destroyInsightCharts();
        if (!LWC || !insightsReady.value) return;
        const d = insights.data;
        const dates = (d.chart && d.chart.dates) || [];
        if (benchEl.value && dates.length >= 2) {
          benchChart = LWC.createChart(benchEl.value, insightChartOptions());
          for (const key of ['portfolio', 'BTC', 'ETH']) {
            const values = d.chart[key];
            if (!values) continue;
            const data = dates.map((time, i) => ({ time, value: values[i] })).filter((p) => p.value !== null && p.value !== undefined);
            if (data.length < 2) continue;
            const line = benchChart.addSeries(LWC.LineSeries, {
              color: BENCH_COLORS[key], lineWidth: key === 'portfolio' ? 3 : 2,
              priceLineVisible: false, lastValueVisible: true, title: key === 'portfolio' ? 'You' : key,
            });
            line.setData(data);
          }
          benchChart.timeScale().fitContent();
        }
        const uw = (d.drawdown && d.drawdown.underwater) || [];
        if (underwaterEl.value && uw.length >= 2) {
          underwaterChart = LWC.createChart(underwaterEl.value, insightChartOptions());
          const base = underwaterChart.addSeries(LWC.BaselineSeries, {
            baseValue: { type: 'price', price: 0 },
            topLineColor: 'rgba(163, 173, 186, 0.6)', topFillColor1: 'rgba(0,0,0,0)', topFillColor2: 'rgba(0,0,0,0)',
            bottomLineColor: COLORS.down, bottomFillColor1: hexToRgba(COLORS.down, 0.05), bottomFillColor2: hexToRgba(COLORS.down, 0.32),
            priceLineVisible: false, lastValueVisible: true,
            priceFormat: { type: 'custom', formatter: (v) => v.toFixed(2) + '%', minMove: 0.01 },
          });
          base.setData(uw.map((p) => ({ time: p.date, value: p.drawdown_pct })));
          underwaterChart.timeScale().fitContent();
        }
      }

      // -- sections ------------------------------------------------------------
      function setTab(tab) {
        if (SECTIONS.includes(tab)) activeTab.value = tab;
      }

      // Left/right arrows move between the two tabs (WAI-ARIA tabs pattern).
      function onTabKeydown(event) {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        const next = SECTIONS[(SECTIONS.indexOf(activeTab.value) + 1) % SECTIONS.length];
        setTab(next);
        nextTick(() => { const el = document.getElementById('tab-' + next); if (el) el.focus(); });
      }

      // Charts in a section that was display: none come back at zero or stale width. Once the
      // section is laid out again (next frame), let every chart re-measure and re-fit.
      function refitCharts(tab) {
        requestAnimationFrame(() => {
          window.dispatchEvent(new Event('resize'));
          if (tab === 'portfolio') {
            if (chart) chart.timeScale().fitContent();
            return;
          }
          if (insightChartsStale) { drawInsightCharts(); return; }
          if (benchChart) benchChart.timeScale().fitContent();
          if (underwaterChart) underwaterChart.timeScale().fitContent();
        });
      }

      watch(activeTab, async (tab, previous) => {
        tabScroll[previous] = window.scrollY;
        onChartRelease();
        await nextTick();
        window.scrollTo({ top: tabScroll[tab] || 0, behavior: 'instant' });
        if (tab === 'analytics' && !why.data && !why.loading && !insights.data && !insights.loading
            && portfolio.value.origin !== 'empty') {
          loadAnalytics();
        }
        refitCharts(tab);
      });

      // -- peek sheet ("Why it moved", 24h) ------------------------------------------
      async function loadPeek() {
        const seq = ++peekSeq;
        peek.loading = true;
        try {
          const data = await api(`/api/portfolio/attribution?window=24h&top=${PEEK_TOP}`);
          if (seq !== peekSeq) return;
          peek.data = data;
          peek.error = '';
        } catch (error) {
          if (seq !== peekSeq) return;
          handleError(error, { silent: true });
          peek.data = null;
          peek.error = error.status === 401 ? '' : 'Could not explain the move: ' + error.message;
        } finally {
          if (seq === peekSeq) peek.loading = false;
        }
      }

      async function openPeek() {
        lastFocus = document.activeElement;
        peek.open = true;
        document.body.classList.add('sheet-open');
        if (!peek.data || peek.error) loadPeek();
        await nextTick();
        if (peekClose.value) peekClose.value.focus();
      }

      function closePeek({ restoreFocus = true } = {}) {
        if (!peek.open) return;
        peek.open = false;
        if (!settingsOpen.value) document.body.classList.remove('sheet-open');
        if (restoreFocus && lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
      }

      // "See full analysis": same 24h move, explained in full on the Analytics tab.
      function peekToAnalytics() {
        closePeek({ restoreFocus: false });
        setPeriod('24H');
        setTab('analytics');
        nextTick(() => { const el = document.getElementById('tab-analytics'); if (el) el.focus({ preventScroll: true }); });
      }

      function onPeekKeydown(event) {
        if (event.key === 'Escape') { event.preventDefault(); closePeek(); return; }
        trapFocus(event, peekSheet.value);
      }

      async function refresh({ silent = false } = {}) {
        if (refreshing.value) return;
        refreshing.value = true;
        try {
          portfolio.value = normalise(await api('/api/portfolio/refresh', { method: 'POST' }));
          await loadHistory({ force: true });
          loadAnalytics();
          peek.data = null;   // a new snapshot changes the 24h story
          if (peek.open) loadPeek();
          if (!silent) {
            const failed = portfolio.value.errors.length;
            showToast(failed ? `Updated, but ${failed} ${failed === 1 ? 'source' : 'sources'} failed` : 'Balances updated',
              failed ? 'error' : 'ok');
          }
        } catch (error) {
          if (error.detail && Array.isArray(error.detail.errors)) {
            portfolio.value = { ...portfolio.value, errors: error.detail.errors };
            showErrors.value = true;
          }
          handleError(error, { silent: silent && error.status !== 401 && !error.detail });
        } finally {
          refreshing.value = false;
          nowMs.value = Date.now();
        }
      }

      async function init() {
        loading.value = true;
        try {
          await loadPortfolio();
        } catch (error) {
          handleError(error);
        } finally {
          loading.value = false;
        }
        if (locked.value) return;
        await nextTick();
        ensureChart();
        await loadHistory();
        // Analytics is fetched up front (SQLite only, cheap) so the tab opens filled in; its
        // charts are drawn the first time it is shown.
        if (portfolio.value.origin !== 'empty') loadAnalytics();
        const p = portfolio.value;
        if (p.configured && (p.origin === 'empty' || ageMinutes() > STALE_MINUTES)) refresh({ silent: true });
      }

      // -- chart ---------------------------------------------------------------
      function ensureChart() {
        if (chart || !chartEl.value || !LWC) return;
        chart = LWC.createChart(chartEl.value, {
          autoSize: true,
          layout: {
            background: { type: LWC.ColorType.Solid, color: 'transparent' },
            textColor: '#8590A0',
            fontFamily: 'Manrope, system-ui, sans-serif',
            fontSize: 11,
            attributionLogo: false,   // credited in the page footer instead
          },
          grid: { vertLines: { visible: false }, horzLines: { visible: false } },
          rightPriceScale: { visible: false, scaleMargins: { top: 0.16, bottom: 0.1 } },
          leftPriceScale: { visible: false },
          timeScale: { visible: false, borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
          crosshair: {
            mode: LWC.CrosshairMode.Magnet,
            vertLine: { color: 'rgba(233, 237, 242, 0.32)', width: 1, style: LWC.LineStyle.Solid, labelVisible: false },
            horzLine: { visible: false, labelVisible: false },
          },
          handleScroll: false,
          handleScale: false,
        });
        series = chart.addSeries(LWC.AreaSeries, {
          lineWidth: 2,
          lineType: LWC.LineType.Simple,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerRadius: 5,
          crosshairMarkerBorderWidth: 2,
          crosshairMarkerBorderColor: '#161B22',
        });
        touchSurface = chartWrap.value;
        if (touchSurface) touchSurface.addEventListener('touchmove', onChartTouchMove, { passive: false });
        drawChart();
      }

      function drawChart() {
        if (!series) return;
        const trend = rangeChange.value ? rangeChange.value.trend : 'flat';
        const color = COLORS[trend];
        series.applyOptions({
          lineColor: color,
          topColor: hexToRgba(color, 0.26),
          bottomColor: hexToRgba(color, 0),
          crosshairMarkerBackgroundColor: color,
          lineVisible: points.value.length >= 2,
        });
        series.setData(points.value.length >= 2 ? points.value.map((p) => ({ time: p.time, value: p.total_usd })) : []);
        chart.timeScale().fitContent();
      }

      function destroyChart() {
        if (touchSurface) touchSurface.removeEventListener('touchmove', onChartTouchMove);
        touchSurface = null;
        endTouch();
        scrub.value = null;
        if (chart) chart.remove();
        chart = null;
        series = null;
      }

      // Scrubbing. The wrapper has `touch-action: pan-y`, so a vertical swipe that starts on the
      // chart scrolls the page (the browser sends pointercancel). A touch only starts scrubbing
      // once it has clearly moved sideways or rested in place, so scrolling past the chart never
      // flashes a tooltip or changes the headline number.
      let touch = null;   // { id, x, y, lastX, active, timer } for the one finger that scrubs

      function scrubAt(clientX) {
        const pts = points.value;
        if (!chart || !series || pts.length < 2 || !chartWrap.value) return;
        const rect = chartWrap.value.getBoundingClientRect();
        const logical = chart.timeScale().coordinateToLogical(clientX - rect.left);
        if (logical === null) return;
        const point = pts[nearestSnapshot(pts, Math.min(pts.length - 1, Math.max(0, logical)))];
        chart.setCrosshairPosition(point.total_usd, point.time, series);
        const x = chart.timeScale().timeToCoordinate(point.time);
        const half = Math.min(70, rect.width / 2);
        const tipX = Math.min(rect.width - half, Math.max(half, x === null ? clientX - rect.left : x));
        scrub.value = {
          value: point.total_usd,
          when: fmt.when(point.time),
          tipX,
          first: pts[0].total_usd,
          firstDay: fmt.day(pts[0].time),
        };
      }

      // Index of the real snapshot closest to fractional index `at`. Filler points only keep the
      // X axis proportional (an interpolated value nobody recorded), so the scrub skips them.
      function nearestSnapshot(pts, at) {
        let best = Math.round(at);
        if (!pts[best].filled) return best;
        let bestDist = Infinity;
        for (let lo = Math.floor(at), hi = Math.ceil(at); lo >= 0 || hi < pts.length; lo--, hi++) {
          if (lo >= 0 && !pts[lo].filled && at - lo < bestDist) { best = lo; bestDist = at - lo; }
          if (hi < pts.length && !pts[hi].filled && hi - at < bestDist) { best = hi; bestDist = hi - at; }
          if (bestDist <= hi - at && bestDist <= at - lo) break;
        }
        return best;
      }

      function endTouch() {
        if (!touch) return;
        clearTimeout(touch.timer);
        const { id } = touch;
        touch = null;
        try { chartWrap.value.releasePointerCapture(id); } catch (_) { /* already released */ }
      }

      function onChartDown(event) {
        if (event.pointerType === 'mouse') { scrubAt(event.clientX); return; }
        if (touch || points.value.length < 2) return;   // a second finger never steals the scrub
        const current = {
          id: event.pointerId, x: event.clientX, y: event.clientY, lastX: event.clientX, active: false, timer: 0,
        };
        current.timer = setTimeout(() => {
          if (touch !== current) return;
          current.active = true;
          scrubAt(current.lastX);
        }, SCRUB_HOLD_MS);
        touch = current;
        // Capture so the release is seen even when the finger ends up outside the chart.
        try { chartWrap.value.setPointerCapture(event.pointerId); } catch (_) { /* pointer already gone */ }
      }

      function onChartMove(event) {
        if (event.pointerType === 'mouse') { scrubAt(event.clientX); return; }
        if (!touch || event.pointerId !== touch.id) return;
        touch.lastX = event.clientX;
        if (!touch.active) {
          const dx = Math.abs(event.clientX - touch.x);
          const dy = Math.abs(event.clientY - touch.y);
          if (dy > SCRUB_SLOP_PX && dy >= dx) { endTouch(); return; }   // vertical: leave it to the page
          if (dx < SCRUB_SLOP_PX) return;
          clearTimeout(touch.timer);
          touch.active = true;
        }
        scrubAt(event.clientX);
      }

      // pointerup, pointercancel (the browser took the gesture to scroll), lost capture, window blur
      function onChartRelease(event) {
        if (event && event.pointerType === 'mouse' && event.type === 'pointerup') return;   // still hovering
        if (event && touch && event.pointerId !== touch.id) return;
        endTouch();
        if (!scrub.value) return;
        scrub.value = null;
        if (chart) chart.clearCrosshairPosition();
      }

      function onChartLeave(event) {
        if (event.pointerType === 'mouse') onChartRelease();
      }

      // Once scrubbing has started, keep the page still even if the finger drifts vertically.
      function onChartTouchMove(event) {
        if (touch && touch.active && event.cancelable) event.preventDefault();
      }

      // -- UI actions ----------------------------------------------------------
      function setRange(id) {
        if (rangeId.value === id) return;
        rangeId.value = id;
        storage.set(STORE.range, id);
        onChartRelease();
        loadHistory();
      }

      function togglePrivacy() {
        privacy.value = !privacy.value;
        storage.set(STORE.privacy, privacy.value ? '1' : '0');
      }

      function toggleAsset(symbol) {
        expanded.value = expanded.value === symbol ? null : symbol;
      }

      function coinIcon(symbol) {
        const key = String(symbol).toLowerCase();
        if (iconFailed[symbol] || !COIN_ICONS.has(key)) return null;
        return `/static/coins/${key}.svg`;
      }

      async function unlock() {
        unlocking.value = true;
        unlockError.value = '';
        try {
          await api('/api/auth', { method: 'POST', body: { token: unlockToken.value } });
          unlockToken.value = '';
          locked.value = false;
          await nextTick();
          await init();
        } catch (error) {
          unlockError.value = error.status === 401 ? 'That token is not right.' : error.message;
        } finally {
          unlocking.value = false;
        }
      }

      // -- settings ------------------------------------------------------------
      const formSnapshot = () => JSON.stringify({
        binance: { ...form.binance },
        wallets: form.wallets.map((w) => ({ name: w.name, address: w.address, chains: [...w.chains] })),
      });
      const dirty = computed(() => settings.value !== null && formSnapshot() !== baseline.value);

      function populateForm(data) {
        settings.value = data;
        form.binance.enabled = !!data.binance.enabled;
        form.binance.api_key = '';
        form.binance.api_secret = '';
        form.binance.clear = false;
        form.wallets = data.wallets.map((w) => ({ name: w.name, address: w.address, type: w.type, chains: [...w.chains] }));
        baseline.value = formSnapshot();
        binanceTest.state = 'idle';
        binanceTest.result = null;
        draft.value = null;
        draftError.value = '';
        saveError.value = '';
        showKey.value = false;
        showSecret.value = false;
      }

      async function openSettings() {
        closePeek({ restoreFocus: false });
        lastFocus = document.activeElement;
        settingsOpen.value = true;
        document.body.classList.add('sheet-open');
        await nextTick();
        if (sheetClose.value) sheetClose.value.focus();
        try {
          populateForm(await api('/api/settings'));
        } catch (error) {
          handleError(error);
          if (error.status === 401) closeSettings(true);
        }
      }

      function closeSettings(force = false) {
        if (force !== true && dirty.value && !window.confirm('Discard your unsaved changes?')) return;
        settingsOpen.value = false;
        if (!peek.open) document.body.classList.remove('sheet-open');
        if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
      }

      function onSheetKeydown(event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeSettings();
          return;
        }
        trapFocus(event, sheet.value);
      }

      // Keep Tab inside an open sheet.
      function trapFocus(event, container) {
        if (event.key !== 'Tab' || !container) return;
        const focusable = [...container.querySelectorAll('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')]
          .filter((el) => el.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }

      async function saveSettings() {
        if (!settings.value || saving.value || draft.value || saveBlocker.value) return;
        saving.value = true;
        saveError.value = '';
        const b = form.binance;
        const body = {
          binance: {
            enabled: b.enabled,
            api_key: b.clear ? '' : (b.api_key || null),
            api_secret: b.clear ? '' : (b.api_secret || null),
          },
          wallets: form.wallets.map((w) => ({ name: w.name, address: w.address, chains: w.chains })),
        };
        try {
          const data = await api('/api/settings', { method: 'POST', body });
          populateForm(data);
          closeSettings(true);
          showToast('Settings saved');
          historyCache.clear();
          await loadPortfolio().catch((error) => handleError(error, { silent: true }));
          if (data.binance.configured || data.wallets.length) refresh();
        } catch (error) {
          if (error.status === 401) { handleError(error); return; }
          saveError.value = error.message;
        } finally {
          saving.value = false;
        }
      }

      const binanceStatus = computed(() => {
        const s = settings.value;
        if (binanceTest.state === 'testing') return { cls: 'busy', text: 'Testing…' };
        const r = binanceTest.result;
        if (r) {
          if (r.ok) return r.warnings && r.warnings.length ? { cls: 'warn', text: 'Connected · review permissions' } : { cls: 'ok', text: 'Connected' };
          if (r.status === 'unreachable') return { cls: 'error', text: 'Unreachable' };
          if (r.status === 'no_read') return { cls: 'error', text: 'No read permission' };
          return { cls: 'error', text: 'Rejected' };
        }
        if (!form.binance.enabled) return { cls: '', text: 'Off' };
        if (form.binance.clear) return { cls: 'warn', text: 'Key will be removed' };
        if (s && s.binance.configured) return { cls: '', text: 'Saved · not tested' };
        return { cls: '', text: 'Not connected' };
      });

      const canTestBinance = computed(() => {
        const s = settings.value;
        if (!s || form.binance.clear) return false;
        return !!(form.binance.api_key || s.binance.has_api_key) && !!(form.binance.api_secret || s.binance.has_api_secret);
      });

      // Inline feedback for one credential field: null when there is nothing to say.
      function credentialCheck(value, otherValue, hasSaved, noun, otherNoun) {
        if (form.binance.clear) return null;
        if (!value) {
          if (!otherValue) return null;
          return hasSaved
            ? { tone: 'hint', text: `The saved ${noun} stays in use with the new ${otherNoun}.` }
            : { tone: 'error', text: `Paste the ${noun} too. Binance needs both.` };
        }
        if (/\s/.test(value)) return { tone: 'error', text: `Remove the spaces or line breaks inside the ${noun}.` };
        if (/[^A-Za-z0-9]/.test(value)) {
          return { tone: 'warn', text: 'Binance keys contain only letters and digits. Check what was pasted.' };
        }
        if (value.length !== BINANCE_KEY_LENGTH) {
          const problem = value.length < BINANCE_KEY_LENGTH ? 'it may be cut off' : 'it may have extra characters';
          return { tone: 'warn', text: `${value.length} characters. Binance keys have ${BINANCE_KEY_LENGTH}, so ${problem}.` };
        }
        return { tone: 'ok', text: `${BINANCE_KEY_LENGTH} characters, looks complete.` };
      }
      const keyCheck = computed(() => (settings.value
        ? credentialCheck(form.binance.api_key, form.binance.api_secret, settings.value.binance.has_api_key, 'API key', 'secret key')
        : null));
      const secretCheck = computed(() => (settings.value
        ? credentialCheck(form.binance.api_secret, form.binance.api_key, settings.value.binance.has_api_secret, 'secret key', 'API key')
        : null));
      const saveBlocker = computed(() => ([keyCheck.value, secretCheck.value].some((c) => c && c.tone === 'error')
        ? 'Fix the Binance key first' : ''));

      const fieldClass = (check) => ({
        'is-invalid': !!check && check.tone === 'error',
        'is-valid': !!check && check.tone === 'ok',
      });
      const msgIcon = (check) => (check.tone === 'ok' ? '#i-check' : check.tone === 'hint' ? '#i-info' : '#i-alert');

      // A test result describes the keys as they were; editing either one makes it stale.
      watch(() => [form.binance.api_key, form.binance.api_secret], () => {
        if (binanceTest.state === 'testing') return;
        binanceTest.state = 'idle';
        binanceTest.result = null;
      });

      const envBinanceNames = computed(() => (settings.value ? settings.value.env_overrides.filter((n) => n.startsWith('BINANCE_')).join(', ') : ''));

      const permissionList = computed(() => {
        const p = (binanceTest.result && binanceTest.result.permissions) || {};
        return [
          { key: 'reading', label: 'Read', good: true },
          { key: 'spot_trading', label: 'Trade', good: false },
          { key: 'withdrawals', label: 'Withdraw', good: false },
          { key: 'futures', label: 'Futures', good: false },
          { key: 'ip_restricted', label: 'IP locked', good: true },
        ].map((item) => {
          const on = !!p[item.key];
          return { ...item, on, cls: on ? (item.good ? 'on' : 'bad') : '' };
        });
      });

      async function testBinance() {
        binanceTest.state = 'testing';
        binanceTest.result = null;
        try {
          binanceTest.result = await api('/api/settings/test-binance', {
            method: 'POST',
            body: { api_key: form.binance.api_key || null, api_secret: form.binance.api_secret || null },
          });
        } catch (error) {
          if (error.status === 401) { handleError(error); return; }
          binanceTest.result = { ok: false, status: 'error', message: error.message, permissions: {}, warnings: [] };
        } finally {
          binanceTest.state = 'done';
        }
      }

      function clearBinance() {
        form.binance.clear = true;
        form.binance.api_key = '';
        form.binance.api_secret = '';
        binanceTest.result = null;
        binanceTest.state = 'idle';
      }

      // wallets
      const chainColor = (id) => CHAIN_COLORS[id] || '#A3ADBA';
      const chainShort = (id) => {
        const chain = settings.value && settings.value.chains.find((c) => c.id === id);
        return chain ? chain.short : id.toUpperCase();
      };
      const evmChainIds = () => (settings.value ? settings.value.chains.filter((c) => c.type === 'evm').map((c) => c.id) : []);

      const draftKind = computed(() => {
        const address = draft.value ? draft.value.address : '';
        if (EVM_RE.test(address)) return 'evm';
        if (SOLANA_RE.test(address)) return 'solana';
        return null;
      });
      const addressKey = (a) => (EVM_RE.test(a) ? a.toLowerCase() : a);
      const draftDuplicate = computed(() => {
        const d = draft.value;
        if (!d || !draftKind.value) return false;
        return form.wallets.some((w, i) => i !== d.index && addressKey(w.address) === addressKey(d.address));
      });

      // Feedback under the address field. While an address is still being typed it stays neutral
      // (a running count); it only turns into an error for something that can never become valid,
      // or once the field has been left or submitted unfinished.
      const addressCheck = computed(() => {
        const d = draft.value;
        const a = d ? d.address : '';
        const pending = (text, problem) => (d.touched || d.submitted ? { tone: 'error', text: problem } : { tone: 'hint', text });
        if (!a) {
          return d && d.submitted ? { tone: 'error', text: 'Enter an EVM (0x…) or Solana address.' }
            : { tone: 'hint', text: 'EVM (0x…) or Solana address. Public address only.' };
        }
        if (draftDuplicate.value) return { tone: 'error', text: 'This address is already in the list.' };
        if (draftKind.value === 'evm') return { tone: 'ok', text: 'EVM address. Choose the chains to scan.' };
        if (draftKind.value === 'solana') return { tone: 'ok', text: 'Solana address. SOL and SPL tokens are read.' };
        if (/^0x/i.test(a) || a === '0') {
          if (/^0X/.test(a)) return { tone: 'error', text: 'Start the address with a lowercase 0x.' };
          if (/[^0-9a-fA-F]/.test(a.slice(2))) {
            return { tone: 'error', text: 'After 0x only 0-9 and a-f are allowed. Check for a mistyped character.' };
          }
          if (a.length > 42) {
            return { tone: 'error', text: `${a.length} characters, ${a.length - 42} too many. An EVM address has 42.` };
          }
          return pending(`${a.length} of 42 characters`, `Only ${a.length} of 42 characters. The address looks cut off.`);
        }
        if (/[^1-9A-HJ-NP-Za-km-z]/.test(a)) {
          return { tone: 'error', text: 'Not an address. EVM starts with 0x; Solana never contains 0, O, I, l or symbols.' };
        }
        if (a.length > 44) {
          return { tone: 'error', text: `${a.length} characters is too long. A Solana address has 32 to 44.` };
        }
        return pending(`${a.length} characters so far. A Solana address has 32 to 44.`,
          `Only ${a.length} characters. A Solana address has 32 to 44, and an EVM one starts with 0x.`);
      });

      watch(draftKind, (kind) => {
        const d = draft.value;
        if (!d) return;
        d.test = null;
        if (kind === 'solana') d.chains = ['solana'];
        else if (kind === 'evm') {
          d.chains = d.chains.filter((c) => c !== 'solana');
          if (!d.chains.length) d.chains = evmChainIds();
        }
      });

      const chainAllowed = (chain) => !draftKind.value || chain.type === draftKind.value;

      function toggleChain(id) {
        const d = draft.value;
        if (!d) return;
        const order = settings.value.chains.map((c) => c.id);
        d.chains = d.chains.includes(id) ? d.chains.filter((c) => c !== id) : [...d.chains, id].sort((a, b) => order.indexOf(a) - order.indexOf(b));
        d.test = null;
        draftError.value = '';
      }

      async function focusDraft() {
        await nextTick();
        if (!draftEl.value) return;
        draftEl.value.scrollIntoView({ block: 'nearest' });   // smooth unless reduced motion (style.css)
        const input = draftEl.value.querySelector('input');
        if (input) input.focus({ preventScroll: true });
      }

      function focusAddress() {
        const input = draftEl.value && draftEl.value.querySelector('#wallet-address');
        if (input) input.focus();
      }

      function newWallet() {
        draftError.value = '';
        draft.value = { index: -1, name: '', address: '', chains: [], test: null, testing: false, touched: false, submitted: false };
        focusDraft();
      }

      function editWallet(index) {
        const w = form.wallets[index];
        draftError.value = '';
        armedDelete.value = -1;
        draft.value = { index, name: w.name, address: w.address, chains: [...w.chains], test: null, testing: false, touched: false, submitted: false };
        focusDraft();
      }

      function cancelDraft() {
        draft.value = null;
        draftError.value = '';
      }

      function applyDraft() {
        const d = draft.value;
        const kind = draftKind.value;
        if (!d) return;
        d.submitted = true;
        draftError.value = '';
        // The reason is already written under the address field: put the cursor back in it.
        if (!kind || draftDuplicate.value) { focusAddress(); return; }
        const chains = kind === 'solana' ? ['solana'] : d.chains.filter((c) => c !== 'solana');
        if (!chains.length) { draftError.value = 'Pick at least one chain to scan.'; return; }
        const entry = { name: d.name.trim(), address: d.address, type: kind, chains };
        if (d.index === -1) form.wallets.push(entry);
        else form.wallets.splice(d.index, 1, entry);
        draft.value = null;
        draftError.value = '';
      }

      function deleteWallet(index) {
        clearTimeout(armTimer);
        if (armedDelete.value === index) {
          form.wallets.splice(index, 1);
          armedDelete.value = -1;
          if (draft.value && draft.value.index === index) draft.value = null;
          return;
        }
        armedDelete.value = index;
        armTimer = setTimeout(() => { armedDelete.value = -1; }, 3500);
      }

      async function testWallet() {
        const d = draft.value;
        if (!d || !draftKind.value) return;
        d.testing = true;
        d.test = null;
        try {
          d.test = await api('/api/settings/test-wallet', { method: 'POST', body: { address: d.address, chains: d.chains } });
        } catch (error) {
          if (error.status === 401) { handleError(error); return; }
          d.test = { ok: false, reachable: 0, message: error.message, results: [] };
        } finally {
          d.testing = false;
        }
      }

      // -- lifecycle -----------------------------------------------------------
      const onScroll = () => { scrolled.value = window.scrollY > 4; };
      const onVisible = () => {
        if (document.visibilityState !== 'visible' || locked.value || loading.value) return;
        nowMs.value = Date.now();
        if (portfolio.value.configured && ageMinutes() > STALE_MINUTES) refresh({ silent: true });
      };

      // Bindings on the mount element are ignored by Vue, so privacy lives on <body>.
      watch(privacy, (on) => document.body.classList.toggle('privacy', on), { immediate: true });
      watch(tabId, (id) => { storage.set(STORE.tab, id); expanded.value = null; });
      watch(locked, (isLocked) => { if (isLocked) { closePeek({ restoreFocus: false }); destroyChart(); destroyInsightCharts(); } });

      const onBlur = () => onChartRelease();
      const noop = () => {};

      onMounted(() => {
        window.addEventListener('scroll', onScroll, { passive: true });
        window.addEventListener('blur', onBlur);
        // iOS Safari only applies :active styles on touch when a touchstart listener exists.
        document.addEventListener('touchstart', noop, { passive: true });
        document.addEventListener('visibilitychange', onVisible);
        tickTimer = setInterval(() => { nowMs.value = Date.now(); }, 30000);
        init();
      });

      onBeforeUnmount(() => {
        window.removeEventListener('scroll', onScroll);
        window.removeEventListener('blur', onBlur);
        document.removeEventListener('touchstart', noop);
        document.removeEventListener('visibilitychange', onVisible);
        clearInterval(tickTimer);
        destroyChart();
        destroyInsightCharts();
      });

      return {
        fmt, ranges: RANGES, tabs: TABS,
        loading, refreshing, portfolio, points, rangeId, tabId, privacy, scrub, expanded, showErrors,
        iconFailed, toast, scrolled, locked, unlockToken, unlockError, unlocking,
        heroParts, heroChange, heroTrend, scrubLabel, rangeChange, rangeLabel, chartMessage, updatedAgo,
        visibleAssets, valueCol, emptyTitle, emptyHint,
        activeTab, setTab, onTabKeydown,
        periods: ANALYTICS_PERIODS, periodId, analyticsPeriod, setPeriod,
        why, whyReady, whyBuckets, whyRows, whyOthers, whyMessage, whyTrend, whySpan,
        insights, insightsReady, insightMessage, insightTiles, benchLegend, benchEl, underwaterEl,
        peek, peekSheet, peekClose, peekReady, peekTrend, peekSpan, peekSpanNote, peekBuckets, peekRows, peekMessage,
        openPeek, closePeek, peekToAnalytics, onPeekKeydown,
        chartWrap, chartEl, sheet, sheetClose, draftEl,
        refresh, setRange, togglePrivacy, toggleAsset, coinIcon, onChartDown, onChartMove, onChartRelease, onChartLeave, unlock,
        settingsOpen, settings, form, showKey, showSecret, saving, saveError, binanceTest, dirty,
        binanceStatus, canTestBinance, envBinanceNames, permissionList,
        keyCheck, secretCheck, saveBlocker, fieldClass, msgIcon, addressCheck, focusAddress,
        openSettings, closeSettings, onSheetKeydown, saveSettings, testBinance, clearBinance,
        draft, draftError, draftKind, armedDelete, chainColor, chainShort, chainAllowed,
        toggleChain, newWallet, editWallet, cancelDraft, applyDraft, deleteWallet, testWallet,
      };
    },
  });

  app.mount('#app');

  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { /* offline shell is optional */ });
    });
  }
})();
