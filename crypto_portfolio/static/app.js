/* Crypto Portfolio PWA - Vue 3 (global build) + TradingView Lightweight Charts v5.
 *
 * Talks only to this server's /api. Data flow:
 *   GET  /api/portfolio           instant: last live fetch or newest SQLite snapshot
 *   POST /api/portfolio/refresh   live fetch from Binance + chains, records a snapshot
 *   GET  /api/portfolio/history   snapshot totals for the chart
 *   GET  /api/portfolio/attribution  why the total moved: price vs quantity effect per coin
 *   GET  /api/analytics/quant     Analytics: TWR, base 100 vs BTC/ETH, max drawdown, volatility
 *   GET  /api/analytics/allocation  target vs actual weights + suggested orders (?contribution=,
 *                                   or ?source_bucket=&funding_amount_usd=&strategy= for partial / DCA)
 *   POST /api/settings/allocation   the Allocation assistant's ON/OFF switch
 *   GET  /api/cash-flows            Activity timeline: deposits, withdrawals, transfers, Earn by day
 *   GET  /api/cash-flows/{id}/candidates, PATCH /api/cash-flows/{id}   manual transfer confirmation
 *
 * Three sections behind a bottom tab bar: Portfolio (glance: total, chart, assets), Analytics
 * (attribution, performance, risk, allocation; one period selector) and Activity (the cash-flow
 * timeline, fetched the first time the tab opens). Sections are kept mounted (v-show) so charts
 * survive a switch; they are re-fitted when their section shows.
 *   GET/POST /api/settings (+ test-binance, test-wallet) for the settings sheet, manual assets included
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
    { id: 'personal', label: 'Personal', hint: 'Everything off the exchange: on-chain wallets, cold wallets, staking, manual assets' },
  ];
  const SOURCE_ORDER = ['binance_spot', 'binance_earn', 'binance_funding', 'onchain', 'manual'];
  const BINANCE_SOURCES = ['binance_spot', 'binance_earn', 'binance_funding'];
  // Sources each asset-list tab shows. A location is filtered by the source it was read from
  // (`locations_by_source`), never guessed from its spelling: a manual label has no `chain/`.
  const TAB_SOURCES = { binance: BINANCE_SOURCES, personal: ['onchain', 'manual'], all: SOURCE_ORDER };
  // Tabs whose list can be read by coin or by wallet (Binance account / physical wallet).
  const VIEW_SWITCH_TABS = ['binance', 'personal'];
  // Tab ids that no longer exist, and the tab that replaced them (a stored choice survives)
  const RETIRED_TABS = { onchain: 'personal', others: 'personal', wallets: 'personal' };
  const ASSET_VIEWS = [{ id: 'coins', label: 'Coins' }, { id: 'wallets', label: 'Wallets' }];
  const EARN_PRODUCTS = { flexible: 'Flexible', locked: 'Locked' };
  const CHAIN_LABELS = {
    ethereum: 'Ethereum', bsc: 'BNB Chain', arbitrum: 'Arbitrum', base: 'Base', polygon: 'Polygon', solana: 'Solana',
  };
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
    period: 'portfolio.analytics.period', actPeriod: 'portfolio.activity.period',
    actSource: 'portfolio.activity.source', actKind: 'portfolio.activity.kind', assetView: 'portfolio.assetView',
    folds: 'portfolio.analytics.folds' };
  const FOLDS = ['why', 'perf', 'risk', 'basis', 'earn', 'alloc'];   // Analytics cards, in page order (A1)
  const BUYS_SHOWN = 5;   // buys listed before "Show all" (N4)
  const STALE_MS = 130 * 60 * 1000;   // two missed hourly snapshots, plus slack (N2.4)
  const SECTIONS = ['portfolio', 'analytics', 'activity'];
  const SECTION_TITLES = { portfolio: 'Portfolio', analytics: 'Analytics', activity: 'Activity' };
  // Activity tab (M4): look-back, then two independent filters (source x kind) with counts.
  const ACTIVITY_PERIODS = [
    { id: '30D', days: 30, label: 'past 30 days' },
    { id: '90D', days: 90, label: 'past 90 days' },
    { id: '1Y', days: 365, label: 'past year' },
    { id: 'ALL', days: 0, label: 'all history' },
  ];
  const ACTIVITY_SOURCES = [
    { id: 'all', label: 'All' },
    { id: 'binance', label: 'Binance' },
    { id: 'onchain', label: 'On-chain' },
  ];
  const ACTIVITY_KINDS = [
    { id: 'deposit', label: 'Deposits', hint: 'Money coming in', icon: '#i-down' },
    { id: 'withdrawal', label: 'Withdrawals', hint: 'Money going out', icon: '#i-up' },
    { id: 'transfer', label: 'Transfers', hint: 'Between your own accounts', icon: '#i-swap' },
    { id: 'reward', label: 'Earn', hint: 'Simple Earn interest', icon: '#i-sparkle' },
    { id: 'trade', label: 'Trades', hint: 'Spot trades and conversions; inferred from the snapshots where Binance history has none', icon: '#i-swap' },
    { id: 'unexplained', label: 'Unexplained', hint: 'Quantity changes no recorded flow accounts for', icon: '#i-alert' },
  ];
  // Direction is always carried by an arrow or a sign, never by colour alone (M4.4 AC3).
  const KIND_META = {
    deposit: { icon: '#i-down', label: 'Deposit' },
    withdrawal: { icon: '#i-up', label: 'Withdrawal' },
    transfer: { icon: '#i-swap', label: 'Transfer' },
    reward: { icon: '#i-sparkle', label: 'Earn' },
    fee: { icon: '#i-up', label: 'Fee' },
    trade: { icon: '#i-swap', label: 'Inferred trade' },
    unexplained: { icon: '#i-alert', label: 'Unexplained change' },
  };
  const CHANNEL_LABELS = { fiat: 'Bank', fiat_payment: 'Card', p2p: 'P2P', onchain: 'On-chain', manual: 'Manual', earn: 'Earn' };
  const ACTIVITY_PAGE = 50;
  const UNDO_MS = 5000;
  const MANUAL_KINDS = [
    { id: 'cold_wallet', label: 'Cold wallet' },
    { id: 'staking', label: 'Staking' },
    { id: 'other', label: 'Other' },
  ];
  const SYMBOL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$/;
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
    groups: { all: 0, binance: 0, personal: 0 }, wallets: { binance: [], personal: [] }, tab_dust: {},
    source_labels: {}, change_24h: null,
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
      const storedTab = RETIRED_TABS[storage.get(STORE.tab)] || storage.get(STORE.tab);
      const tabId = ref(TABS.some((t) => t.id === storedTab) ? storedTab : 'all');
      const assetView = ref(storage.get(STORE.assetView) === 'wallets' ? 'wallets' : 'coins');
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
      const tabScroll = { portfolio: 0, analytics: 0, activity: 0 };   // each section keeps its own scroll position

      // Analytics: one period for attribution, performance and risk
      const periodId = ref(ANALYTICS_PERIODS.some((p) => p.id === storage.get(STORE.period)) ? storage.get(STORE.period) : '30D');
      const analyticsPeriod = computed(() => ANALYTICS_PERIODS.find((p) => p.id === periodId.value) || ANALYTICS_PERIODS[2]);
      const why = reactive({ data: null, loading: false, error: '' });
      let whySeq = 0;
      const insights = reactive({ data: null, loading: false, error: '' });
      const earn = reactive({ data: null, loading: false, error: '' });
      const basis = reactive({ data: null, loading: false, error: '', allBuys: false });
      const openFolds = storage.get(STORE.folds, 'why').split(',');
      const folds = reactive(Object.fromEntries(FOLDS.map((id) => [id, openFolds.includes(id)])));
      const benchEl = ref(null);
      const underwaterEl = ref(null);
      let insightSeq = 0;
      // Allocation card (M7): `applied` is the contribution in force, `amount` what is typed;
      // the `fund*` fields are the same pair for the Partial / DCA mode (money moved from `source`)
      const alloc = reactive({ data: null, loading: false, error: '', errors: [], toggling: false,
        mode: 'rebalance', amount: '', applied: null, amountError: '', includeDry: false,
        source: '', fundAmount: '', fundApplied: null, fundError: '', strategy: 'greedy' });
      let allocSeq = 0;
      let benchChart = null;
      let underwaterChart = null;
      let insightChartsStale = false;   // data arrived while Analytics was hidden: draw on show

      // Activity tab (M4): fetched the first time the tab opens, then on every filter change
      const pick = (list, key, fallback) => (list.some((x) => x.id === storage.get(key)) ? storage.get(key) : fallback);
      const act = reactive({
        data: null, items: [], loading: false, loadingMore: false, error: '', loaded: false,
        period: pick(ACTIVITY_PERIODS, STORE.actPeriod, '90D'),
        source: pick(ACTIVITY_SOURCES, STORE.actSource, 'all'),
        kind: storage.get(STORE.actKind) && ACTIVITY_KINDS.some((k) => k.id === storage.get(STORE.actKind)) ? storage.get(STORE.actKind) : 'all',
        review: false,
        open: {},       // Earn days expanded, by item key
        match: null,    // { id, loading, list, error }: candidate list open under one item
        busy: null,     // id of the flow being confirmed
        undo: null,     // { text, id } for UNDO_MS after a confirmation
      });
      let actSeq = 0;
      let undoTimer = null;

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
      const form = reactive({ binance: { enabled: true, api_key: '', api_secret: '', clear: false }, wallets: [], manual: [] });
      const baseline = ref('');
      const showKey = ref(false);
      const showSecret = ref(false);
      const saving = ref(false);
      const saveError = ref('');
      const binanceTest = reactive({ state: 'idle', result: null });
      const draft = ref(null);
      const draftError = ref('');
      const armedDelete = ref(-1);
      const manualDraft = ref(null);
      const armedManual = ref(-1);
      const manualBaseline = ref('');

      // template refs
      const chartWrap = ref(null);
      const chartEl = ref(null);
      const sheet = ref(null);
      const sheetClose = ref(null);
      const draftEl = ref(null);
      const manualEl = ref(null);

      let chart = null;
      let series = null;
      let capitalSeries = null;
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
          wallets: { ...base.wallets, ...(data.wallets || {}) },
          tab_dust: data.tab_dust || base.tab_dust,
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

      // N8: what was put in, and what it became. Read on the newest point that has the figure.
      const capital = computed(() => {
        const pts = points.value;
        const last = pts.length ? pts[pts.length - 1] : null;
        if (!last || last.net_deposited_usd === null || last.net_deposited_usd === undefined) return null;
        const gain = last.total_usd - last.net_deposited_usd;
        return { deposited: last.net_deposited_usd, gain, trend: trendOf(gain) };
      });
      // Both lines share one price scale so the gap means something. Over a short range the NAV
      // barely moves while the gap is large: drawing the capital line would flatten the NAV into
      // a stripe, so it is only drawn when the NAV keeps at least a third of the height.
      const capitalFits = computed(() => {
        const pts = points.value.filter((p) => p.net_deposited_usd !== null && p.net_deposited_usd !== undefined);
        if (pts.length < 2) return false;
        const navs = points.value.map((p) => p.total_usd);
        const all = navs.concat(pts.map((p) => p.net_deposited_usd));
        const span = Math.max(...all) - Math.min(...all);
        return span > 0 && (Math.max(...navs) - Math.min(...navs)) / span >= 1 / 3;
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
      const dataStale = computed(() => !!portfolio.value.timestamp
        && nowMs.value - new Date(portfolio.value.timestamp).getTime() > STALE_MS);

      // -- computed: attribution ---------------------------------------------------
      // What made the total move: market price vs holdings change, per coin (§2 of docs/SPEC.md).
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
      // Where an asset sits, for the tab's sources only. On-chain names read `chain/wallet`; a
      // manual label is tagged as such so "My Ledgy" the cold wallet is not mistaken for the
      // on-chain wallet of the same name. Old payloads without the split fall back to nothing
      // rather than to a guess.
      function assetLocations(asset, sources) {
        const bySource = asset.locations_by_source || {};
        const out = [];
        for (const source of sources) {
          for (const name of bySource[source] || []) {
            const text = source === 'manual' ? `${name} (manual)` : name;
            if (!out.includes(text)) out.push(text);
          }
        }
        return out;
      }

      const visibleAssets = computed(() => {
        const p = portfolio.value;
        const tab = tabId.value;
        const groupTotal = num(p.groups[tab]);
        const labels = p.source_labels || {};
        const minValue = num(p.min_value);
        const sources = TAB_SOURCES[tab] || SOURCE_ORDER;
        if (walletView.value) return [];   // the Wallets view renders visibleWallets

        return p.assets
          .map((asset) => {
            let quantity = num(asset.quantity);
            let value = num(asset.value_usd);
            let pct = num(asset.pct);
            if (tab === 'binance') {
              quantity = num(asset.binance_quantity);
              value = num(asset.binance_value_usd);
              pct = groupTotal > 0 ? (value / groupTotal) * 100 : 0;
            } else if (tab === 'personal') {
              quantity = num(asset.personal_quantity);
              value = num(asset.personal_value_usd);
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
              locations: assetLocations(asset, sources),
            };
          })
          .filter((a) => a.quantity > 0 && (tab === 'all' || a.value >= minValue))
          .sort((a, b) => b.value - a.value || a.symbol.localeCompare(b.symbol));
      });

      // Coins view: the tab's small balances, folded into one line (server-side `split_dust`)
      const tabDust = computed(() => (portfolio.value.tab_dust || {})[tabId.value] || portfolio.value.dust);
      // Binance / Personal tabs, "Wallets" view: one row per Binance account or per physical
      // wallet, `pct` of the tab's own total. A Personal wallet lists its on-chain coins, then
      // the manual assets linked to it (tagged Manual); an unlinked manual asset is a row of its
      // own. Rows under the dust filter arrive already folded into each wallet's `dust`.
      const hasViewSwitch = computed(() => VIEW_SWITCH_TABS.includes(tabId.value));
      const walletView = computed(() => hasViewSwitch.value && assetView.value === 'wallets');
      const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
      function walletSub(w) {
        if (tabId.value === 'binance') return plural(num(w.asset_count), 'asset');
        if (w.type === 'manual') return `Manual · ${manualKindLabel(w.manual_kind)}`;
        const chains = Object.keys(w.chains || {});
        return chains.length ? plural(chains.length, 'chain') : 'No on-chain balance';
      }
      const visibleWallets = computed(() => {
        if (!walletView.value) return [];
        const tab = tabId.value;
        return ((portfolio.value.wallets || {})[tab] || [])
          .map((w) => {
            const linked = (w.manual || []).map((m) => ({
              key: `manual:${m.label}:${m.symbol}`, symbol: m.symbol, quantity: num(m.quantity),
              value: num(m.value_usd), price: num(m.price_usd),
            }));
            const rows = (w.assets || []).map((a) => ({
              key: `${a.product || a.chain || ''}:${a.symbol}`, symbol: a.symbol, quantity: num(a.quantity),
              value: num(a.value_usd), price: num(a.price_usd),
              where: tab === 'binance' ? (EARN_PRODUCTS[a.product] || a.product || '') : (CHAIN_LABELS[a.chain] || a.chain || 'On-chain'),
            }));
            const chains = Object.keys(w.chains || {});
            const split = Object.entries(w.split || {})
              .map(([product, value]) => ({ label: EARN_PRODUCTS[product] || product, value: num(value) }));
            const dust = w.dust || { assets: 0, value_usd: 0 };
            return {
              key: `w:${w.id}`, id: w.id, label: w.label, value: num(w.value_usd), pct: num(w.pct), sub: walletSub(w),
              chains: chains.map((c) => ({ label: CHAIN_LABELS[c] || c || 'On-chain', value: num(w.chains[c]) })),
              split, rows, linked, dust,
              // an on-chain wallet holding assets declared by hand: "+ manual" badge, own section
              hasLinked: w.type === 'wallet' && (w.linked_manual || []).length > 0,
              manualValue: num(w.manual_value_usd),
            };
          })
          .filter((w) => w.value > 0 || w.rows.length || w.linked.length || num(w.dust.assets));
      });
      function setAssetView(id) {
        assetView.value = id;
        storage.set(STORE.assetView, id);
        expanded.value = null;
      }

      // Width of the value column: wide enough for the longest balance or price in the list, so
      // money is never cut off. Tabular Manrope digits are ~9.3px at 15px/800, ~7.8px at 13px/600.
      const valueCol = computed(() => {
        let width = 104;
        for (const a of visibleAssets.value) {
          width = Math.max(width, fmt.money(a.value).length * 9.3, fmt.price(a.price).length * 7.8);
        }
        for (const w of visibleWallets.value) width = Math.max(width, fmt.money(w.value).length * 9.3);
        return Math.ceil(width) + 'px';
      });

      const emptyTitle = computed(() => {
        if (portfolio.value.origin === 'empty') return 'No balances fetched yet';
        if (tabId.value === 'binance') return walletView.value ? 'No Binance account to show' : 'No Binance balances to show';
        if (tabId.value === 'personal') return walletView.value ? 'No personal wallet to show' : 'No personal balances to show';
        return 'No balances above the dust filter';
      });
      const emptyHint = computed(() => {
        const min = fmt.money(portfolio.value.min_value);
        if (portfolio.value.origin === 'empty') return 'Fetch balances to take your first snapshot.';
        if (tabId.value === 'binance') return walletView.value ? 'Binance is not connected, or every account is empty.' : `Binance is not connected, or nothing there is worth ${min} or more.`;
        if (tabId.value === 'personal') return walletView.value ? 'Add a wallet address or a manual asset (cold wallet, staking) in Settings.' : `No wallet or manual asset is configured, or nothing there is worth ${min} or more.`;
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

      async function loadEarn() {
        earn.loading = true;
        try {
          earn.data = await api('/api/analytics/earn');
          earn.error = '';
        } catch (error) {
          handleError(error, { silent: true });
          earn.data = null;
          earn.error = error.status === 401 ? '' : 'Could not load Earn income: ' + error.message;
        } finally {
          earn.loading = false;
        }
      }

      async function loadBasis() {
        basis.loading = true;
        try {
          basis.data = await api('/api/analytics/cost-basis');
          basis.error = '';
        } catch (error) {
          handleError(error, { silent: true });
          basis.data = null;
          basis.error = error.status === 401 ? '' : 'Could not load the cost basis: ' + error.message;
        } finally {
          basis.loading = false;
        }
      }

      // A unit price: no cents above $1,000 (they are noise there), significant digits under $1.
      const unitPrice = (v) => (num(v) >= 1000 ? fmt.compactMoney(v) : fmt.price(v));
      const basisRows = computed(() => ((basis.data && basis.data.assets) || []).map((a) => {
        const known = a.avg_entry !== null && a.unrealised_usd !== null;
        const share = Math.round(num(a.tracked) * 100);
        return {
          symbol: a.symbol, known, estimated: a.estimated,
          trackedText: !known || share >= 100 ? '' : `${share}% tracked`,
          note: !known ? '' : [a.estimated ? 'deposits priced at that day\'s market' : '', share < 100 ? `${share}% tracked` : ''].filter(Boolean).join(' · '),
          entryText: known ? unitPrice(a.avg_entry) : '', nowText: unitPrice(a.price),
          pnlText: known ? fmt.signedMoney(a.unrealised_usd) : '', pctText: known ? fmt.signedPct(a.unrealised_pct) : '',
          trend: known ? trendOf(num(a.unrealised_usd)) : 'flat',
        };
      }));
      const basisBy = computed(() => Object.fromEntries(basisRows.value.map((r) => [r.symbol, r])));
      const basisTrend = computed(() => (basis.data && basis.data.unrealised_usd !== null ? trendOf(num(basis.data.unrealised_usd)) : 'flat'));
      const basisBuys = computed(() => {
        const buys = (basis.data && basis.data.buys) || [];
        const pp = (v) => (num(v) < 0 ? '−' : '+') + Math.abs(num(v)).toFixed(1) + ' pp';
        return (basis.allBuys ? buys : buys.slice(0, BUYS_SHOWN)).map((b) => ({
          key: b.timestamp + b.symbol, symbol: b.symbol,
          day: new Date(b.timestamp).toLocaleDateString(undefined, { day: 'numeric', month: 'short',
            ...(new Date(b.timestamp).getFullYear() === new Date().getFullYear() ? {} : { year: 'numeric' }) }),
          qtyText: fmt.qty(b.quantity), priceText: (b.estimated ? '≈ ' : '') + unitPrice(b.price),
          sinceText: b.since_pct === null ? '—' : fmt.signedPct(b.since_pct),
          sinceTrend: b.since_pct === null ? 'flat' : trendOf(num(b.since_pct)),
          vsText: b.vs_stable_pp === null ? '—' : pp(b.vs_stable_pp),
          vsTrend: b.vs_stable_pp === null ? 'flat' : trendOf(num(b.vs_stable_pp)),
        }));
      });
      const basisMoreBuys = computed(() => {
        const n = ((basis.data && basis.data.buys) || []).length;
        return !basis.allBuys && n > BUYS_SHOWN ? n : 0;
      });
      const basisBenchNote = computed(() => (basis.data && basis.data.benchmark_apr !== null
        ? `vs USDC Earn: against leaving the same dollars in Earn at today's ${(num(basis.data.benchmark_apr) * 100).toFixed(2)}%.`
        : 'vs USDC Earn needs a stablecoin position in Earn with a listed rate.'));
      const basisMessage = computed(() => {
        if (basis.error) return basis.error;
        if (!basis.data) return '';
        if (!basis.data.trades && !basisRows.value.some((r) => r.known)) {
          return 'No trade is synced yet. The collector (`python main.py record`) reads your Binance trade history after each snapshot; the first run takes a few minutes.';
        }
        return basisRows.value.length ? '' : 'Nothing but stablecoins to price.';
      });

      function loadAnalytics() {
        loadAttribution();
        loadInsights();
        loadBasis();
        loadEarn();
        loadAllocation();
      }

      // -- folding cards (A1) --------------------------------------------------------
      function toggleFold(id) {
        folds[id] = !folds[id];
        storage.set(STORE.folds, FOLDS.filter((f) => folds[f]).join(','));
        if (folds[id] && (id === 'perf' || id === 'risk')) {   // a chart drawn while folded has no width
          insightChartsStale = true;
          nextTick(() => refitCharts('analytics'));
        }
      }

      const aprText = (apr) => (apr === null || apr === undefined ? '—' : (num(apr) * 100).toFixed(2) + '%');
      // `listed_apr` already includes the tier bonus, pro rata of the balance inside each band
      // (clients/binance.py effective_apr); this spells out how: "2.02% base + 3.00% on 0–5,000 USDC".
      const listedBreakdown = (a) => {
        const tiers = a.listed_tiers || [];
        if (a.listed_apr === null || !tiers.length) return '';
        const band = (t) => `${aprText(t.apr)} on ${num(t.min).toLocaleString()}–${num(t.max).toLocaleString()} ${a.symbol}`;
        const base = a.listed_base_apr === null || a.listed_base_apr === undefined ? '' : aprText(a.listed_base_apr) + ' base + ';
        return `Listed rate earned overall: ${base}${tiers.map(band).join(' + ')} (bonus only on the balance inside each band)`;
      };
      const earnRows = computed(() => ((earn.data && earn.data.assets) || []).map((a) => ({
        symbol: a.symbol,
        listedText: a.listed_apr === null ? 'no listed rate' : 'listed ' + aprText(a.listed_apr),
        listedHint: listedBreakdown(a),
        windows: earn.data.windows.map((days) => {
          const w = a.realised[days + 'd'] || {};
          return { id: days + 'd', text: aprText(w.apr), sufficient: !!w.sufficient,
            hint: w.sufficient ? `Rewards over the average balance, ${w.n_obs} snapshots` : `Snapshots cover ${num(w.days).toFixed(1)} of ${days} days` };
        }),
        perDayText: a.usd_per_day === null ? '—' : fmt.money(a.usd_per_day),
      })));
      const earnGapNote = computed(() => (earnRows.value.some((r) => r.windows.some((w) => !w.sufficient))
        ? '— : the snapshots do not cover that window yet, or have a gap in it.' : ''));
      const earnLife = computed(() => {
        const life = earn.data && earn.data.lifetime;
        if (!life || !life.since) return null;
        return {
          since: new Date(life.since).toLocaleDateString(undefined, { month: 'short', year: 'numeric' }),
          total: life.total_usd, stable: life.stable_usd, years: life.by_year, unpriced: life.unpriced,
        };
      });
      const earnMessage = computed(() => {
        if (earn.error) return earn.error;
        if (earn.data && !earnRows.value.length && !earnLife.value) return 'No Simple Earn position or reward recorded yet.';
        return '';
      });

      // What each card says in one line while folded: the figure that answers its question.
      const foldSums = computed(() => {
        const none = (loading) => ({ money: '', text: loading ? '…' : '—', trend: '' });
        const out = {};
        out.why = whyReady.value
          ? { money: fmt.signedMoney(why.data.delta_usd), trend: whyTrend.value,
            text: why.data.delta_pct !== null ? ` (${fmt.signedPct(why.data.delta_pct)})` : '' }
          : none(why.loading);
        const twr = insightsReady.value ? (insights.data.twr || {}).twr_pct : null;
        out.perf = twr !== null && twr !== undefined
          ? { money: '', text: 'TWR ' + fmt.signedPct(twr), trend: trendOf(num(twr)) } : none(insights.loading);
        const mdd = insightsReady.value ? (insights.data.drawdown || {}).max_drawdown_pct : null;
        out.risk = mdd !== null && mdd !== undefined
          ? { money: '', text: (num(mdd) < 0 ? '−' : '') + Math.abs(num(mdd)).toFixed(2) + '% from peak', trend: num(mdd) < 0 ? 'down' : '' } : none(insights.loading);
        const top = earn.data && earn.data.assets[0];
        if (earn.data && earn.data.usd_per_day !== null) {
          const best = top.realised['7d'].apr ?? top.listed_apr;
          out.earn = { money: fmt.money(earn.data.usd_per_day), trend: '',
            text: '/day' + (best !== null && best !== undefined ? ` · ${top.symbol} ${aprText(best)}` : '') };
        } else if (top && top.listed_apr !== null) {
          out.earn = { money: '', text: `${top.symbol} listed ${aprText(top.listed_apr)}`, trend: '' };
        } else if (earnLife.value) {
          out.earn = { money: fmt.money(earnLife.value.total), text: ' received', trend: '' };
        } else out.earn = none(earn.loading);
        out.basis = basis.data && basis.data.unrealised_usd !== null
          ? { money: fmt.signedMoney(basis.data.unrealised_usd), trend: basisTrend.value, text: ` · ${Math.round(num(basis.data.tracked_pct))}% tracked` }
          : (basis.data ? { money: '', text: 'No cost basis yet', trend: '' } : none(basis.loading));
        if (alloc.data && !alloc.data.enabled) out.alloc = { money: '', text: 'Off', trend: '' };
        else if (allocReady.value) {
          const off = allocRows.value.filter((r) => r.breach).length;
          out.alloc = { money: '', text: off ? `${off} off target` : 'On target', trend: '' };
        } else out.alloc = none(alloc.loading);
        return out;
      });

      function setPeriod(id) {
        if (periodId.value === id) return;
        periodId.value = id;
        storage.set(STORE.period, id);
        loadAttribution();  // the allocation is a picture of now: the period does not change it
        loadInsights();
      }

      // -- allocation (Epic M7) ------------------------------------------------------
      async function loadAllocation() {
        const seq = ++allocSeq;
        alloc.loading = true;
        const params = new URLSearchParams();
        if (alloc.mode === 'contribution' && alloc.applied) params.set('contribution', String(alloc.applied));
        if (alloc.mode === 'funding' && alloc.fundApplied && alloc.source) {
          params.set('source_bucket', alloc.source);
          params.set('funding_amount_usd', String(alloc.fundApplied));
          params.set('strategy', alloc.strategy);
        }
        if (alloc.includeDry) params.set('include_dry_powder', 'true');
        const query = params.toString();
        try {
          const data = await api('/api/analytics/allocation' + (query ? '?' + query : ''));
          if (seq !== allocSeq) return;
          alloc.data = data;
          alloc.error = '';
          alloc.errors = [];
          if (!alloc.source) alloc.source = defaultFundingSource(data);
        } catch (error) {
          if (seq !== allocSeq) return;
          handleError(error, { silent: true });
          // a refused funding request (unknown bucket...): keep the card, show it under the field
          if (alloc.mode === 'funding' && error.status === 422 && alloc.data
              && !(error.detail && Array.isArray(error.detail.errors))) {
            alloc.fundError = error.message;
            alloc.fundApplied = null;
            return;
          }
          alloc.data = null;
          alloc.errors = error.status === 422 && error.detail && Array.isArray(error.detail.errors) ? error.detail.errors : [];
          alloc.error = error.status === 401 ? '' : alloc.errors.length ? error.message : 'Could not load the allocation: ' + error.message;
        } finally {
          if (seq === allocSeq) alloc.loading = false;
        }
      }

      async function toggleAllocation(on = !allocEnabled.value) {
        if (alloc.toggling) return;
        alloc.toggling = true;
        try {
          const status = await api('/api/settings/allocation', { method: 'POST', body: { enabled: on } });
          if (settings.value) settings.value = { ...settings.value, allocation: status };
          showToast(on ? 'Allocation assistant on' : 'Allocation assistant off');
          await loadAllocation();
        } catch (error) {
          handleError(error);
        } finally {
          alloc.toggling = false;
        }
      }

      function setAllocMode(mode) {
        if (alloc.mode === mode) return;
        alloc.mode = mode;
        alloc.amountError = '';
        alloc.fundError = '';
        if (mode === 'rebalance' || (mode === 'contribution' && alloc.applied)
            || (mode === 'funding' && alloc.fundApplied)) loadAllocation();
      }

      // Partial / DCA: the source defaults to the stablecoin bucket, else the most liquid one
      function defaultFundingSource(data) {
        const rows = ((data && data.buckets) || []).filter((b) => b.liquid_usd > 0);
        const stable = rows.find((b) => b.stablecoin);
        if (stable) return stable.name;
        return rows.length ? rows.reduce((a, b) => (b.liquid_usd > a.liquid_usd ? b : a)).name : '';
      }

      function setFundingSource(name) {
        if (alloc.source === name) return;
        alloc.source = name;
        alloc.fundError = '';
        if (alloc.fundApplied) loadAllocation();
      }

      function setFundingStrategy(strategy) {
        if (alloc.strategy === strategy) return;
        alloc.strategy = strategy;
        if (alloc.fundApplied) loadAllocation();
      }

      function applyFunding() {
        const raw = String(alloc.fundAmount).replace(/[\s,$]/g, '');
        const value = Number(raw);
        if (!alloc.source) {
          alloc.fundError = 'Pick a source first.';
          return;
        }
        if (!raw || !Number.isFinite(value) || value <= 0) {
          alloc.fundError = 'Enter an amount in USD above 0.';
          return;
        }
        alloc.fundError = '';
        alloc.fundApplied = Math.round(value * 100) / 100;
        loadAllocation();
      }

      // 10 % / 25 % / Max of what the source can sell now (liquid, never the locked part)
      function quickFunding(share) {
        const row = fundingSourceRow.value;
        if (!row) return;
        alloc.fundAmount = String(Math.floor(row.liquid_usd * share * 100) / 100);
        applyFunding();
      }

      function applyContribution() {
        const raw = String(alloc.amount).replace(/[\s,$]/g, '');
        const value = Number(raw);
        if (!raw || !Number.isFinite(value) || value <= 0) {
          alloc.amountError = 'Enter an amount in USD above 0.';
          return;
        }
        alloc.amountError = '';
        alloc.applied = Math.round(value * 100) / 100;
        loadAllocation();
      }

      function toggleDryPowder() {
        alloc.includeDry = !alloc.includeDry;
        loadAllocation();
      }

      async function copyText(text) {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (_) {  // no async clipboard (plain http on the LAN): the old textarea trick
          const area = document.createElement('textarea');
          area.value = text;
          area.setAttribute('readonly', '');
          area.style.position = 'fixed';
          area.style.opacity = '0';
          document.body.appendChild(area);
          area.select();
          let ok = false;
          try { ok = document.execCommand('copy'); } catch (__) { ok = false; }
          document.body.removeChild(area);
          return ok;
        }
      }

      async function copyOrder(action) {
        const ok = await copyText(action.text);
        showToast(ok ? 'Order copied' : 'Could not copy: select the text instead', ok ? 'ok' : 'error');
      }

      async function copyAllOrders() {
        const actions = (alloc.data && alloc.data.actions) || [];
        if (!actions.length) return;
        const ok = await copyText(actions.map((a) => a.text).join('\n'));
        showToast(ok ? `${actions.length} ${actions.length === 1 ? 'order' : 'orders'} copied` : 'Could not copy', ok ? 'ok' : 'error');
      }

      const allocEnabled = computed(() => !!(alloc.data ? alloc.data.enabled : alloc.errors.length));
      const allocReady = computed(() => !!(alloc.data && alloc.data.enabled && alloc.data.available));
      // One scale for every bar, so 10 % and 50 % buckets compare at a glance.
      const allocScale = computed(() => {
        if (!allocReady.value) return 100;
        const top = Math.max(10, ...alloc.data.buckets.map((b) => Math.max(num(b.actual_pct), num(b.band[1]))));
        return Math.min(100, Math.ceil(top / 10) * 10);
      });
      const allocRows = computed(() => {
        if (!allocReady.value) return [];
        const scale = allocScale.value;
        const w = (v) => Math.max(0, Math.min(100, (num(v) / scale) * 100));
        return alloc.data.buckets.map((b) => ({
          ...b,
          actualW: w(b.actual_pct),
          targetW: w(b.target_pct),
          bandL: w(b.band[0]),
          bandW: Math.max(0.5, w(b.band[1]) - w(b.band[0])),
          driftText: (num(b.drift_pp) < 0 ? '−' : '+') + Math.abs(num(b.drift_pp)).toFixed(1) + ' pp',
          flag: b.breach ? (b.direction === 'over' ? 'Over target' : 'Under target') : '',
          lockedNote: b.locked_usd > 0 ? (b.locked_reasons.includes('locked_earn') ? 'in locked Earn' : 'not sellable') : '',
        }));
      });
      const fundingSources = computed(() => (allocReady.value
        ? alloc.data.buckets.filter((b) => b.liquid_usd > 0) : []));
      const fundingSourceRow = computed(() => fundingSources.value.find((b) => b.name === alloc.source) || null);
      // excess = what the source holds above its target (value − target% × total), in USD:
      // "$5,000 = 21% of the Stablecoin excess ($23,800 over its 5% target) · ~4-5 more batches this size to target"
      // "$3,857.84 = 138% of the Others excess ($2,802.20 over its 2% target) · goes $1,055.64 past target"
      const fundingProgress = computed(() => {
        const f = allocReady.value && alloc.data.mode === 'funding' ? alloc.data.funding : null;
        if (!f) return null;
        const src = alloc.data.buckets.find((b) => b.name === f.source_bucket) || null;
        const tgt = src ? `${Number(num(src.target_pct).toFixed(2))}%` : '';  // 2 -> "2%", 2.5 -> "2.5%"
        const over = src ? ` (${fmt.money(f.source_excess_usd)} over its ${tgt} target)` : '';
        const overshoot = Math.round((num(f.applied_usd) - num(f.source_excess_usd)) * 100) / 100;
        let line = '';
        if (f.source_excess_usd <= 0) {
          line = `${f.source_bucket} is at or under its target: no excess left to move.`;
        } else if (f.progress_pct !== null) {
          const n = f.remaining_batches_estimate;
          const lo = Math.floor(n);
          const hi = Math.ceil(n);
          const batches = overshoot >= 0.01 ? `goes ${fmt.money(overshoot)} past target`
            : n <= 1 ? 'this batch reaches target'
              : `~${lo === hi ? lo : lo + '-' + hi} more batches this size to target`;
          line = `${fmt.money(f.applied_usd)} = ${Math.round(f.progress_pct)}% of the ${f.source_bucket} excess${over} · ${batches}`;
        }
        const notes = [];
        if (f.limited_by === 'liquidity') {
          notes.push(`Only ${fmt.money(f.applied_usd)} of ${fmt.money(f.requested_usd)} can move: the rest of ${f.source_bucket} is locked (locked Earn or not sellable).`);
        } else if (f.limited_by === 'room') {
          notes.push(`Only ${fmt.money(f.applied_usd)} of ${fmt.money(f.requested_usd)} is needed: the other buckets are at target, so the rest is not sold.`);
        }
        if (f.source_below_target) {
          const after = src ? ` (${fmt.pct(src.after_pct)} vs ${tgt})` : '';
          const stop = f.source_excess_usd > 0 ? ` Move ${fmt.money(f.source_excess_usd)} to stop exactly at target.` : '';
          notes.push(`After this batch ${f.source_bucket} drops below its target${after}.${stop}`);
        }
        return { line, notes };
      });
      // funding orders only under Partial / DCA, and only funding orders there (no stale list)
      const allocModeMismatch = computed(() => allocReady.value
        && (alloc.mode === 'funding') !== (alloc.data.mode === 'funding'));
      const allocActions = computed(() => {
        if (!allocReady.value || allocModeMismatch.value) return [];
        return alloc.data.actions.map((a) => {
          if (a.quote_spend) {  // a stablecoin source: the buys are paid in it, nothing is sold
            const notes = [a.bucket, 'pay directly on X/' + (a.asset || 'USDC') + ' pairs'];
            if (a.legs.length) notes.push('from ' + [...new Set(a.legs.map((l) => l.from))].join(', '));
            if (a.legs.some((l) => l.requires_redeem)) notes.push('redeem from Earn first');
            if (a.requires_transfer) notes.push('transfer to Binance first');
            return { ...a, key: 'USE:' + a.bucket, side: 'use', sideLabel: 'USE', note: notes.join(' · '),
              what: a.asset || a.bucket };  // read with the badge and the amount: "USE USDC ~$X"
          }
          const notes = [a.bucket];
          if (a.legs.length) notes.push('from ' + [...new Set(a.legs.map((l) => l.from))].join(', '));
          if (a.requires_transfer) notes.push('transfer to Binance first');
          if (a.constrained_by === 'locked_earn') notes.push('capped: the rest is in locked Earn');
          else if (a.constrained_by) notes.push('capped: the rest cannot be sold now');
          return { ...a, key: a.action + ':' + a.bucket, side: a.action === 'BUY' ? 'buy' : 'sell',
            sideLabel: a.action, note: notes.join(' · '),
            what: a.est_qty !== null && a.est_qty !== undefined ? fmt.qty(a.est_qty) + ' ' + a.asset : (a.asset || a.bucket) };
        });
      });
      const allocMessage = computed(() => {
        if (alloc.error) return alloc.error;
        const d = alloc.data;
        if (!d || !d.enabled) return '';
        if (!d.available) {
          return d.reason === 'no_snapshot' ? 'Take a snapshot first: the allocation is read from the newest one.'
            : 'Nothing priced to allocate yet.';
        }
        if (allocModeMismatch.value) return '';
        if (d.mode === 'contribution') return d.actions.length ? '' : 'Nothing to buy with this amount.';
        if (d.mode === 'funding') return d.actions.length ? '' : 'No bucket is under target, so there is nothing to buy.';
        if (!d.rebalance_needed) return `Every bucket is within ±${d.tolerance_pp} pp of its target. Nothing to do.`;
        if (!d.actions.length) return `Some buckets drifted, but every order would be under the $${d.min_trade_usd} minimum.`;
        return '';
      });

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

      // -- activity (Epic M4) --------------------------------------------------------
      const activityPeriod = computed(() => ACTIVITY_PERIODS.find((p) => p.id === act.period) || ACTIVITY_PERIODS[1]);

      async function loadActivity({ append = false } = {}) {
        if (append && !(act.data && act.data.next_cursor)) return;
        const seq = ++actSeq;   // a slower answer for filters the user already left is dropped
        if (append) act.loadingMore = true; else act.loading = true;
        const params = new URLSearchParams({
          days: String(activityPeriod.value.days), source: act.source, type: act.kind,
          limit: String(ACTIVITY_PAGE), tz_offset: String(-new Date().getTimezoneOffset()),
        });
        if (act.review) params.set('review', 'true');
        if (append) params.set('cursor', act.data.next_cursor);
        try {
          const data = await api('/api/cash-flows?' + params.toString());
          if (seq !== actSeq) return;
          act.items = append ? [...act.items, ...data.items] : data.items;
          act.data = data;
          act.error = '';
          act.loaded = true;
        } catch (error) {
          if (seq !== actSeq) return;
          handleError(error, { silent: true });
          if (!append) { act.data = null; act.items = []; }
          act.error = error.status === 401 ? '' : 'Could not load the activity: ' + error.message;
        } finally {
          if (seq === actSeq) { act.loading = false; act.loadingMore = false; }
        }
      }

      function reloadActivity() {
        act.match = null;
        loadActivity();
      }

      function setActPeriod(id) {
        if (act.period === id || !ACTIVITY_PERIODS.some((p) => p.id === id)) return;
        act.period = id;
        storage.set(STORE.actPeriod, id);
        reloadActivity();
      }

      function setActSource(id) {
        if (act.source === id) return;
        act.source = id;
        storage.set(STORE.actSource, id);
        reloadActivity();
      }

      // Kind chips work like radio buttons that can be switched off again: tap the active one for all.
      function setActKind(id) {
        act.kind = act.kind === id ? 'all' : id;
        storage.set(STORE.actKind, act.kind);
        reloadActivity();
      }

      function setActReview(on) {
        if (act.review === on) return;
        act.review = on;
        reloadActivity();
        if (on) nextTick(() => window.scrollTo({ top: 0 }));
      }

      function toggleEarnDay(key) {
        act.open[key] = !act.open[key];
      }

      async function openMatch(item) {
        if (act.match && act.match.id === item.id) { act.match = null; return; }
        const current = { id: item.id, loading: true, list: [], error: '' };
        act.match = current;
        try {
          const data = await api(`/api/cash-flows/${item.id}/candidates`);
          if (act.match && act.match.id === item.id) act.match.list = data.candidates || [];
        } catch (error) {
          handleError(error, { silent: true });
          if (act.match && act.match.id === item.id) act.match.error = error.status === 401 ? '' : error.message;
        } finally {
          if (act.match && act.match.id === item.id) act.match.loading = false;
        }
      }

      async function confirmFlow(item, body, text) {
        if (act.busy) return;
        act.busy = item.id;
        try {
          await api(`/api/cash-flows/${item.id}`, { method: 'PATCH', body });
          act.match = null;
          await loadActivity();
          clearTimeout(undoTimer);
          act.undo = { text, id: item.id };
          undoTimer = setTimeout(() => { act.undo = null; }, UNDO_MS);
        } catch (error) {
          handleError(error);   // nothing was changed server-side: the row keeps its old state
        } finally {
          act.busy = null;
        }
      }

      function markExternal(item) {
        const text = item.kind === 'transfer' ? 'Marked as not a transfer'
          : item.direction === 'out' ? 'Marked as sent outside' : 'Marked as received from outside';
        confirmFlow(item, { is_internal_transfer: false }, text);
      }

      // `candidate` null: "it went to one of my wallets, but its other side is not tracked".
      function pickMatch(item, candidate) {
        confirmFlow(item, { is_internal_transfer: true, matched_flow_id: candidate ? candidate.id : null },
          candidate ? 'Paired as one transfer' : 'Marked as a move to your own wallet');
      }

      function confirmPair(item) {
        confirmFlow(item, { is_internal_transfer: true, matched_flow_id: item.partner_id }, 'Transfer confirmed');
      }

      async function undoConfirm() {
        const undo = act.undo;
        if (!undo) return;
        clearTimeout(undoTimer);
        act.undo = null;
        try {
          await api(`/api/cash-flows/${undo.id}`, { method: 'PATCH', body: { reset: true } });
          await loadActivity();
          showToast('Undone');
        } catch (error) {
          handleError(error);
        }
      }

      const localDay = (ms) => new Date(ms - new Date(ms).getTimezoneOffset() * 60000).toISOString().slice(0, 10);

      function dayLabel(day, today, yesterday) {
        if (day === today) return 'Today';
        if (day === yesterday) return 'Yesterday';
        const d = new Date(day + 'T12:00:00');
        const opts = { weekday: 'short', month: 'short', day: 'numeric' };
        if (d.getFullYear() !== new Date().getFullYear()) { opts.year = 'numeric'; delete opts.weekday; }
        return d.toLocaleDateString(undefined, opts);
      }

      function presentFlow(item) {
        const meta = KIND_META[item.kind] || KIND_META.deposit;
        const isTransfer = item.kind === 'transfer';
        const out = item.direction === 'out';
        const tone = isTransfer ? 'flat' : out ? 'down' : 'up';
        const channel = item.channel === 'crypto' ? (item.network || '') : (CHANNEL_LABELS[item.channel] || '');
        const time = new Date(item.timestamp).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
        let title;
        let route;
        if (item.kind === 'trade') {
          const names = (legs) => legs.map((leg) => leg.asset).join(' + ');
          title = 'Trade';   // the legs can be many: they go on the second line, which may truncate
          route = `${names(item.sold)} → ${names(item.bought)}`;
          // What one coin cost is the fact a trade is read for; the total is already on the row.
          // A leg worth $1 a unit is the money side: it has no price worth showing.
          const priced = item.assets.map((leg) => {
            const unit = leg.amount ? Math.abs(num(leg.value_usd) / leg.amount) : 0;
            return { ...leg, priceText: unit > 0 && Math.abs(unit - 1) > 0.005 ? '@ ' + unitPrice(unit) : '' };
          });
          const coins = priced.filter((leg) => leg.priceText);
          if (coins.length === 1) route += ' ' + coins[0].priceText;
          item = { ...item, assets: priced };
        } else if (item.kind === 'unexplained') {
          const n = item.assets.length;
          title = 'Unexplained';
          route = n === 1 ? `${item.assets[0].amount < 0 ? '−' : '+'}${fmt.qty(Math.abs(item.assets[0].amount))} ${item.assets[0].asset}` : `${n} quantity changes`;
        } else if (item.group) {
          const n = item.assets.length;
          title = 'Earn';
          route = n === 1 ? `${fmt.qty(item.assets[0].amount)} ${item.assets[0].asset}` : `${n} assets`;
          if (item.count > n) route += ` · ${item.count} payouts`;
        } else {
          title = `${fmt.qty(item.amount)} ${item.asset}`;
          if (isTransfer) route = `${item.from} → ${item.to}`;
          else if (item.kind === 'reward') route = 'Earn reward';
          else if (out) route = item.to && item.to !== 'External' ? `${meta.label} → ${item.to}` : [meta.label, channel].filter(Boolean).join(' · ');
          else route = item.from && item.from !== 'External' ? `${meta.label} from ${item.from}` : [meta.label, channel].filter(Boolean).join(' · ');
        }
        const sign = isTransfer ? '' : out ? '−' : '+';
        const value = num(item.value_usd);
        if (item.kind === 'trade' || item.kind === 'unexplained') {
          // no gain/loss colour: a trade moves value, it does not add any. "≈" marks an estimate.
          const label = item.kind === 'trade' && !item.inferred ? 'Trade' : meta.label;
          return {
            ...item, icon: meta.icon, kindLabel: label, tone: 'flat', title, route, time,
            amountText: item.kind === 'trade' ? (item.inferred || item.estimated ? '≈ ' : '') + fmt.money(value) : fmt.signedMoney(value),
            feeText: '', pending: false, reviewText: '', canPair: false, txShort: '',
            sr: `${label}, ${title}, ${route}`,
          };
        }
        return {
          ...item, icon: meta.icon, kindLabel: meta.label, tone, title, route, time,
          amountText: value > 0 ? sign + fmt.money(value) : (isTransfer ? '' : 'No price'),
          feeText: num(item.fee_usd) >= 0.01 ? '−' + fmt.money(item.fee_usd) + ' fee' : '',
          pending: item.status === 'pending',
          reviewText: !item.needs_review ? '' : isTransfer ? 'Check pairing' : 'Unmatched',
          canPair: isTransfer && !!item.partner_id,
          txShort: item.tx_hash ? fmt.shortAddress(item.tx_hash, 6) : '',
          sr: `${meta.label}, ${title}, ${route}${value > 0 ? ', ' + sign + fmt.money(value) : ''}`,
        };
      }

      const actGroups = computed(() => {
        const now = Date.now();
        const today = localDay(now);
        const yesterday = localDay(now - 86400000);
        const groups = [];
        for (const item of act.items) {
          let group = groups[groups.length - 1];
          if (!group || group.day !== item.day) {
            group = { day: item.day, label: dayLabel(item.day, today, yesterday), items: [] };
            groups.push(group);
          }
          group.items.push(presentFlow(item));
        }
        // N9: a note is the first thing of its day, and a day can have a note and nothing else.
        // Today is always there, so there is a place to write. Older note-only days wait until
        // the items around them are loaded, or they would sit in the wrong place.
        const notes = (act.data && act.data.notes) || {};
        const oldest = act.data && act.data.next_cursor && groups.length ? groups[groups.length - 1].day : '';
        const unfiltered = act.source === 'all' && act.kind === 'all' && !act.review;
        const days = new Set(Object.keys(notes).filter((day) => day >= oldest));
        if (act.data && unfiltered) days.add(today);
        for (const day of days) {
          if (!groups.some((g) => g.day === day)) groups.push({ day, label: dayLabel(day, today, yesterday), items: [] });
        }
        groups.sort((a, b) => (a.day < b.day ? 1 : -1));
        for (const group of groups) group.note = notes[group.day] || '';
        return groups;
      });

      const noteEdit = reactive({ day: '', text: '', saving: false, error: '' });
      function openNote(group) {
        Object.assign(noteEdit, { day: group.day, text: group.note, saving: false, error: '' });
        nextTick(() => { const el = document.getElementById('note-' + group.day); if (el) el.focus(); });
      }
      function closeNote() {
        noteEdit.day = '';
      }
      async function saveNote(text = noteEdit.text) {
        noteEdit.saving = true;
        try {
          const note = await api('/api/notes/' + noteEdit.day, { method: 'PUT', body: { body: text } });
          const notes = { ...((act.data && act.data.notes) || {}) };
          if (note.body) notes[note.day] = note.body; else delete notes[note.day];
          if (act.data) act.data.notes = notes;
          closeNote();
          loadAttribution();   // "Why it moved" quotes the notes of its period
        } catch (error) {
          handleError(error, { silent: true });
          noteEdit.error = 'Could not save the note: ' + error.message;
        } finally {
          noteEdit.saving = false;
        }
      }
      const whyNotes = computed(() => {
        const now = Date.now();
        return ((why.data && why.data.notes) || []).map((n) => ({
          ...n, label: dayLabel(n.day, localDay(now), localDay(now - 86400000)),
        }));
      });

      const actFacet = (axis, id) => {
        const facets = act.data && act.data.facets;
        return facets && facets[axis] ? num(facets[axis][id]) : null;
      };

      const actSummary = computed(() => {
        const d = act.data;
        if (!d) return null;
        const net = num(d.net_external_usd);
        return { net, netTrend: trendOf(net), earned: num(d.earned_usd) };
      });

      const actMessage = computed(() => {
        if (act.error || act.loading || !act.data) return '';
        if (act.items.length) return '';
        if (act.review) return 'Nothing waits for a confirmation. Every transfer is matched.';
        if (!act.data.synced) return 'Binance history is not synced yet. The collector (`python main.py record`) pulls up to 5 years of deposits, withdrawals and Earn interest after each snapshot.';
        const filtered = act.source !== 'all' || act.kind !== 'all';
        return `No activity in the ${activityPeriod.value.label}${filtered ? ' for these filters' : ''}.`;
      });

      // -- sections ------------------------------------------------------------
      function setTab(tab) {
        if (SECTIONS.includes(tab)) activeTab.value = tab;
      }

      // Left/right arrows cycle through the tabs, Home/End jump to the ends (WAI-ARIA tabs pattern).
      function onTabKeydown(event) {
        const at = SECTIONS.indexOf(activeTab.value);
        const moves = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: SECTIONS.length - 1 };
        if (!(event.key in moves)) return;
        event.preventDefault();
        const next = SECTIONS[(moves[event.key] + SECTIONS.length) % SECTIONS.length];
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
          if (tab !== 'analytics') return;
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
        // Activity is fetched only when first opened, so it never slows down the Portfolio load.
        if (tab === 'activity' && !act.loaded && !act.loading) loadActivity();
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
          if (act.loaded) loadActivity();   // an edited manual quantity is a new flow
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
        // N8: net deposited capital, dashed and quiet; the gap to the area above it is the gain
        capitalSeries = chart.addSeries(LWC.LineSeries, {
          color: '#8590A0', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
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
        capitalSeries.setData(capitalFits.value
          ? points.value.filter((p) => p.net_deposited_usd !== null && p.net_deposited_usd !== undefined)
            .map((p) => ({ time: p.time, value: p.net_deposited_usd })) : []);
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
        capitalSeries = null;
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

      // `symbol` is a coin, or `w:<id>` for a row of the Wallets view: the prefix keeps them apart
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
      const manualSnapshot = () => JSON.stringify(form.manual.map((a) => ({
        label: a.label, kind: a.kind, linked_wallet: a.linked_wallet || null,
        holdings: a.holdings.map((h) => ({ symbol: h.symbol, quantity: num(h.quantity), price_id: h.price_id || '',
          price_usd: h.price_usd ?? null, liquid: h.liquid !== false })),
      })));
      const formSnapshot = () => JSON.stringify({
        binance: { ...form.binance },
        wallets: form.wallets.map((w) => ({ name: w.name, address: w.address, chains: [...w.chains] })),
        manual: manualSnapshot(),
      });
      const dirty = computed(() => settings.value !== null && formSnapshot() !== baseline.value);

      function populateForm(data) {
        settings.value = data;
        form.binance.enabled = !!data.binance.enabled;
        form.binance.api_key = '';
        form.binance.api_secret = '';
        form.binance.clear = false;
        form.wallets = data.wallets.map((w) => ({ name: w.name, address: w.address, type: w.type, chains: [...w.chains] }));
        form.manual = (data.manual_assets || []).map((a) => ({
          label: a.label, kind: a.kind, linked_wallet: a.linked_wallet || '',
          updated_at: a.updated_at, age_days: a.age_days, review_hint: a.review_hint,
          holdings: a.holdings.map((h) => ({ symbol: h.symbol, quantity: num(h.quantity), price_id: h.price_id || '',
            price_usd: h.price_usd ?? null, liquid: h.liquid !== false })),
        }));
        manualBaseline.value = manualSnapshot();
        manualDraft.value = null;
        armedManual.value = -1;
        baseline.value = formSnapshot();
        binanceTest.state = 'idle';
        binanceTest.result = null;
        draft.value = null;
        draftError.value = '';
        saveError.value = '';
        showKey.value = false;
        showSecret.value = false;
      }

      // -- backups ---------------------------------------------------------------
      const backup = reactive({ data: null, busy: false, error: '' });
      async function loadBackup() {
        try {
          backup.data = await api('/api/backup');
          backup.error = '';
        } catch (error) {
          handleError(error, { silent: true });
          backup.error = error.status === 401 ? '' : 'Could not read the backup status: ' + error.message;
        }
      }
      async function backupNow() {
        backup.busy = true;
        try {
          backup.data = await api('/api/backup', { method: 'POST' });
          backup.error = '';
          portfolio.value = { ...portfolio.value, backup_overdue: backup.data.overdue };
        } catch (error) {
          handleError(error, { silent: true });
          backup.error = 'The backup did not run: ' + error.message;
        } finally {
          backup.busy = false;
        }
      }
      const backupView = computed(() => {
        const b = backup.data;
        if (!b) return null;
        const run = b.last_run;
        const error = b.config_error || (run && run.error) || '';
        const state = !b.enabled ? ['Off', ''] : error ? ['Needs a look', 'warn'] : b.overdue ? ['Overdue', 'warn'] : b.latest ? ['Up to date', 'ok'] : ['None yet', 'warn'];
        return {
          word: state[0], tone: state[1], error,
          latest: b.latest ? `${fmt.ago(b.latest_at, nowMs.value)} · ${(num(b.latest_size) / 1e6).toFixed(1)} MB · ${b.count} kept` : 'No backup yet',
          schedule: b.enabled ? `every ${b.every_hours} h, after a snapshot · newest ${b.keep} kept` : 'off (backup.enabled is false in config.yaml)',
          remote: !b.remote ? 'none, local copies only'
            : `${b.remote}${run && run.uploaded === true ? ' · last upload worked' : run && run.uploaded === false ? ' · last upload failed' : ''}`,
        };
      });

      async function openSettings() {
        closePeek({ restoreFocus: false });
        lastFocus = document.activeElement;
        settingsOpen.value = true;
        document.body.classList.add('sheet-open');
        await nextTick();
        if (sheetClose.value) sheetClose.value.focus();
        loadBackup();
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
        const focusable = [...container.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')]
          .filter((el) => el.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }

      async function saveSettings() {
        if (!settings.value || saving.value || draft.value || manualDraft.value || saveBlocker.value) return;
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
        // Only when edited: an entry config.yaml has but cannot parse (see manual_errors) must not
        // be dropped by a save that was about something else.
        if (manualSnapshot() !== manualBaseline.value) body.manual_assets = JSON.parse(manualSnapshot());
        try {
          const data = await api('/api/settings', { method: 'POST', body });
          populateForm(data);
          closeSettings(true);
          showToast('Settings saved');
          historyCache.clear();
          await loadPortfolio().catch((error) => handleError(error, { silent: true }));
          if (data.binance.configured || data.wallets.length || (data.manual_assets || []).length) refresh();
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
      const saveBlocker = computed(() => {
        if ([keyCheck.value, secretCheck.value].some((c) => c && c.tone === 'error')) return 'Fix the Binance key first';
        const orphan = form.manual.find((a) => a.linked_wallet && !walletOptions.value.includes(a.linked_wallet));
        return orphan ? `Re-link "${orphan.label}": wallet "${orphan.linked_wallet}" is gone` : '';
      });

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
        const before = d.index === -1 ? null : walletLabel(form.wallets[d.index]);
        if (d.index === -1) form.wallets.push(entry);
        else form.wallets.splice(d.index, 1, entry);
        // a renamed wallet takes its linked manual assets along, unless another wallet keeps the old name
        const after = walletLabel(entry);
        if (before && before !== after && !walletOptions.value.includes(before)) {
          form.manual.forEach((a) => { if (a.linked_wallet === before) a.linked_wallet = after; });
        }
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

      // manual assets (M6): balances no API can read - a hardware wallet, a validator, another exchange
      const manualKindLabel = (id) => (MANUAL_KINDS.find((k) => k.id === id) || MANUAL_KINDS[2]).label;
      // The label config.yaml stores for a wallet (the backend default when no name is given).
      const walletLabel = (w) => (w.name || '').trim() || `${w.address.slice(0, 6)}..${w.address.slice(-4)}`;
      // Configured wallets a manual asset can be linked to, each label once (EVM and Solana
      // keys of one Ledger may share a name: same device).
      const walletOptions = computed(() => [...new Set(form.wallets.map(walletLabel))]);
      const manualSummary = (asset) => asset.holdings.map((h) => `${fmt.qty(h.quantity)} ${h.symbol}`).join(' · ') || 'No balances';
      const manualAge = (asset) => {
        if (asset.age_days === null || asset.age_days === undefined) return '';
        return asset.age_days === 0 ? 'updated today' : `updated ${asset.age_days} d ago`;
      };

      async function focusManual(selector = 'input') {
        await nextTick();
        if (!manualEl.value) return;
        manualEl.value.scrollIntoView({ block: 'nearest' });
        const input = manualEl.value.querySelector(selector);
        if (input) input.focus({ preventScroll: true });
      }

      const blankHolding = (symbol = '') => ({ symbol, quantity: '', price_id: '', price_usd: null, liquid: true, invalid: '' });

      function newManual() {
        armedManual.value = -1;
        manualDraft.value = { index: -1, label: '', kind: 'cold_wallet', linked_wallet: '', holdings: [blankHolding('BTC')], error: '', labelError: false };
        focusManual();
      }

      function editManual(index) {
        const a = form.manual[index];
        armedManual.value = -1;
        manualDraft.value = {
          index, label: a.label, kind: a.kind, linked_wallet: a.linked_wallet || '', error: '', labelError: false,
          holdings: a.holdings.map((h) => ({ ...h, quantity: String(h.quantity), invalid: '' })),
        };
        focusManual('.holding-qty input');
      }

      function cancelManual() {
        manualDraft.value = null;
      }

      function addHolding() {
        const d = manualDraft.value;
        if (!d) return;
        d.holdings.push(blankHolding());
        focusManual('.holding-row:last-child input');
      }

      function removeHolding(index) {
        const d = manualDraft.value;
        if (d) d.holdings.splice(index, 1);
      }

      // "0,85" and "1 000.5" are what a phone keypad or a copy from a wallet app produce.
      const parseQty = (value) => {
        const text = String(value ?? '').trim().replace(/\s/g, '').replace(',', '.');
        return text === '' ? NaN : Number(text);
      };

      function applyManual() {
        const d = manualDraft.value;
        if (!d) return;
        d.error = '';
        d.labelError = false;
        if (d.linked_wallet && !walletOptions.value.includes(d.linked_wallet)) {
          d.error = `Wallet "${d.linked_wallet}" is no longer configured. Pick another one or "None".`;
          focusManual('#manual-link');
          return;
        }
        // Linked to a wallet: the name may stay empty and defaults to the wallet's, no retyping.
        const label = d.label.trim() || d.linked_wallet;
        if (!label) { d.error = 'Give it a name, e.g. Ledger or Lido.'; d.labelError = true; focusManual('#manual-label'); return; }
        if (form.manual.some((a, i) => i !== d.index && a.label.toLowerCase() === label.toLowerCase())) {
          d.error = `"${label}" is already in the list. Names must be unique.`;
          d.labelError = true;
          focusManual('#manual-label');
          return;
        }
        const seen = new Set();
        let firstBad = -1;
        d.holdings.forEach((h, i) => {
          const symbol = h.symbol.trim().toUpperCase();
          const qty = parseQty(h.quantity);
          h.invalid = !SYMBOL_RE.test(symbol) ? 'symbol'
            : seen.has(symbol) ? 'duplicate'
              : !Number.isFinite(qty) || qty < 0 ? 'quantity' : '';
          seen.add(symbol);
          if (h.invalid && firstBad === -1) firstBad = i;
        });
        if (!d.holdings.length) { d.error = 'Add at least one balance.'; return; }
        if (firstBad !== -1) {
          const bad = d.holdings[firstBad];
          d.error = bad.invalid === 'symbol' ? 'Enter a ticker such as BTC (letters and digits).'
            : bad.invalid === 'duplicate' ? `${bad.symbol.trim().toUpperCase()} is listed twice.`
              : 'Enter a quantity of 0 or more, e.g. 0.85.';
          focusManual(`.holding-row:nth-child(${firstBad + 1}) ${bad.invalid === 'quantity' ? '.holding-qty ' : ''}input`);
          return;
        }
        const previous = d.index === -1 ? null : form.manual[d.index];
        const entry = {
          label, kind: d.kind, linked_wallet: d.linked_wallet || '',
          updated_at: previous ? previous.updated_at : null,
          age_days: previous ? previous.age_days : null,
          review_hint: previous ? previous.review_hint : null,
          holdings: d.holdings.map((h) => ({ symbol: h.symbol.trim().toUpperCase(), quantity: parseQty(h.quantity),
            price_id: h.price_id || '', price_usd: h.price_usd ?? null, liquid: h.liquid !== false })),
        };
        if (d.index === -1) form.manual.push(entry);
        else form.manual.splice(d.index, 1, entry);
        manualDraft.value = null;
      }

      function deleteManual(index) {
        clearTimeout(armTimer);
        if (armedManual.value === index) {
          form.manual.splice(index, 1);
          armedManual.value = -1;
          if (manualDraft.value && manualDraft.value.index === index) manualDraft.value = null;
          return;
        }
        armedManual.value = index;
        armTimer = setTimeout(() => { armedManual.value = -1; }, 3500);
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
        clearTimeout(undoTimer);
        destroyChart();
        destroyInsightCharts();
      });

      return {
        fmt, ranges: RANGES, tabs: TABS,
        loading, refreshing, portfolio, points, rangeId, tabId, privacy, scrub, expanded, showErrors,
        assetViews: ASSET_VIEWS, assetView, hasViewSwitch, walletView, visibleWallets, setAssetView, tabDust,
        iconFailed, toast, scrolled, locked, unlockToken, unlockError, unlocking,
        heroParts, heroChange, heroTrend, scrubLabel, rangeChange, rangeLabel, chartMessage, updatedAgo, capital, capitalFits,
        visibleAssets, valueCol, emptyTitle, emptyHint,
        activeTab, setTab, onTabKeydown, sectionTitles: SECTION_TITLES,
        act, activityPeriods: ACTIVITY_PERIODS, activitySources: ACTIVITY_SOURCES, activityKinds: ACTIVITY_KINDS,
        activityPeriod, actGroups, actFacet, actSummary, actMessage, loadActivity, reloadActivity,
        setActPeriod, setActSource, setActKind, setActReview, toggleEarnDay,
        openMatch, markExternal, pickMatch, confirmPair, undoConfirm,
        periods: ANALYTICS_PERIODS, periodId, analyticsPeriod, setPeriod,
        why, whyReady, whyBuckets, whyRows, whyOthers, whyMessage, whyTrend, whySpan,
        folds, toggleFold, foldSums, earn, earnRows, earnGapNote, earnLife, earnMessage, dataStale,
        noteEdit, openNote, closeNote, saveNote, whyNotes,
        backup, backupView, backupNow,
        basis, basisRows, basisBy, basisTrend, basisBuys, basisMoreBuys, basisBenchNote, basisMessage,
        insights, insightsReady, insightMessage, insightTiles, benchLegend, benchEl, underwaterEl,
        alloc, allocEnabled, allocReady, allocRows, allocActions, allocMessage, loadAllocation, toggleAllocation,
        setAllocMode, applyContribution, toggleDryPowder, copyOrder, copyAllOrders,
        fundingSources, fundingSourceRow, fundingProgress, setFundingSource, setFundingStrategy, applyFunding, quickFunding,
        peek, peekSheet, peekClose, peekReady, peekTrend, peekSpan, peekSpanNote, peekBuckets, peekRows, peekMessage,
        openPeek, closePeek, peekToAnalytics, onPeekKeydown,
        chartWrap, chartEl, sheet, sheetClose, draftEl, manualEl,
        refresh, setRange, togglePrivacy, toggleAsset, coinIcon, onChartDown, onChartMove, onChartRelease, onChartLeave, unlock,
        settingsOpen, settings, form, showKey, showSecret, saving, saveError, binanceTest, dirty,
        binanceStatus, canTestBinance, envBinanceNames, permissionList,
        keyCheck, secretCheck, saveBlocker, fieldClass, msgIcon, addressCheck, focusAddress,
        openSettings, closeSettings, onSheetKeydown, saveSettings, testBinance, clearBinance,
        draft, draftError, draftKind, armedDelete, chainColor, chainShort, chainAllowed,
        toggleChain, newWallet, editWallet, cancelDraft, applyDraft, deleteWallet, testWallet,
        manualKinds: MANUAL_KINDS, walletOptions, manualDraft, armedManual, manualKindLabel, manualSummary, manualAge,
        newManual, editManual, cancelManual, addHolding, removeHolding, applyManual, deleteManual,
      };
    },
  });

  // The one "N balances under $1 hidden" line, shared by every tab's Coins list and by each
  // wallet's detail in the Wallets view: same rule (server-side `split_dust`), same words.
  app.component('dust-note', {
    props: { dust: { type: Object, default: null }, min: { type: Number, default: 0 } },
    setup: () => ({ fmt }),
    template: `<p v-if="dust && dust.assets" class="text-[12px] text-ink-3">
      {{ dust.assets }} {{ dust.assets === 1 ? 'balance' : 'balances' }} under {{ fmt.money(min) }} hidden
      (<span class="priv priv-soft num">{{ fmt.money(dust.value_usd) }}</span>, counted in the total).
    </p>`,
  });

  app.mount('#app');

  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { /* offline shell is optional */ });
    });
  }
})();
