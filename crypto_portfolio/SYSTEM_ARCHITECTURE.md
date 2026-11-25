# SYSTEM_ARCHITECTURE — `crypto_portfolio`

> **Phạm vi:** hiện trạng hệ thống tại ngày 2026-11-25 (branch `claude/script-preparation-docs-944f7e`).
> Tài liệu được viết sau khi đọc toàn bộ mã nguồn (`*.py`, `static/app.js`, `static/index.html`,
> `static/sw.js`, `PRODUCT.md`, `deploy/`) và kiểm tra môi trường chạy thật (cron, systemd, cổng
> đang lắng nghe, nội dung `portfolio_history.db` ở chế độ read-only). Các nhận định có ghi
> **[đã kiểm chứng]** là đã đo trên máy. Các nhận định ghi **[suy luận]** chưa đo được trực tiếp.
>
> Tài liệu đi kèm: [`ROADMAP_LEDGER.md`](ROADMAP_LEDGER.md) (đặc tả tính năng tương lai + sổ theo dõi).

---

## Mục lục

1. [Tổng quan & triết lý kiến trúc](#1-tổng-quan--triết-lý-kiến-trúc)
2. [Chi tiết thành phần](#2-chi-tiết-thành-phần-component-deep-dive)
3. [Sơ đồ luồng dữ liệu](#3-sơ-đồ-luồng-dữ-liệu)
4. [An ninh & bảo mật](#4-an-ninh--bảo-mật)
5. [Vận hành & triển khai](#5-vận-hành--triển-khai)
6. [Kết quả thẩm định: rủi ro & sai lệch](#6-kết-quả-thẩm-định-rủi-ro--sai-lệch-so-với-mô-tả)
7. [Phụ lục: hợp đồng dữ liệu (data contracts)](#7-phụ-lục-hợp-đồng-dữ-liệu)

---

## 1. Tổng quan & triết lý kiến trúc

### 1.1 Mục đích sản phẩm

`crypto_portfolio` gom **mọi tài sản crypto của một người** về một con số Net Worth (USD) duy nhất,
kèm lịch sử theo thời gian. Nguồn dữ liệu gồm:

| Nhóm | Nguồn | Module |
|---|---|---|
| CEX | Binance **Spot**, **Simple Earn** (flexible + locked), **Funding** | `clients/binance.py` |
| Self-custody EVM | Ethereum, BSC, Arbitrum, Base, Polygon: native coin + ERC-20 đã cấu hình | `clients/onchain.py` |
| Self-custody Solana | SOL + mọi SPL token (cả Token-2022) | `clients/onchain.py` |
| Giá | Binance ticker (CEX), DefiLlama `coins.llama.fi` (on-chain), đều không cần key | cả hai client |

Tiêu chí thành công (theo `PRODUCT.md`): mở app lên là đọc được tổng tài sản và biến động 24h trong
vòng một giây, và mọi con số đều truy ngược được về nguồn.

### 1.2 Định vị: self-hosted, read-only, local-first

| Nguyên tắc | Cách code bảo đảm |
|---|---|
| **Read-only theo cấu trúc** | Binance client chỉ có các hàm *đọc* (`/api/v3/account`, `/sapi/v1/simple-earn/*/position`, `/sapi/v1/asset/get-funding-asset`, `/sapi/v1/account/apiRestrictions`). Không có endpoint order/transfer/withdraw nào. Key chỉ cần quyền *Enable Reading*. Màn hình Settings còn **cảnh báo** khi key có quyền trade/withdraw (`web_api.test_binance`). |
| **Không private key** | On-chain chỉ dùng địa chỉ public (`eth_getBalance`, `eth_call balanceOf`, `getTokenAccountsByOwner`). Không ký giao dịch nào. |
| **Local-first** | Dữ liệu nằm trong SQLite trên máy. Chỉ có các truy vấn số dư và giá đi ra ngoài. Không telemetry, không CDN: Vue, Tailwind, Lightweight Charts và font đều vendored. |
| **Không nói dối im lặng** | Lỗi từng phần được báo kèm con số. Dust bị ẩn khỏi danh sách nhưng **vẫn tính vào tổng** và **vẫn lưu**. Token không đọc được `decimals()` thì **bỏ qua**, không đoán. Tài sản không có giá thì giữ $0, không định giá phỏng đoán. |

### 1.3 Nguyên tắc phân tách trách nhiệm (decoupling)

Hệ thống chia thành **ba mặt phẳng** (plane) độc lập. Chúng chỉ giao tiếp qua file SQLite:

```
 ┌──────────────────────┐     ┌────────────────────────┐     ┌─────────────────────────┐
 │ 1. COLLECTOR PLANE    │     │ 2. PRESENTATION PLANE   │     │ 3. EXTERNAL GATEWAY     │
 │ main.py record (cron) │     │ web_api.py + static/    │     │ api_gateway.py          │
 │ ghi snapshot           │     │ PWA nội bộ (LAN)        │     │ Cloudflare Tunnel       │
 │ biết Binance key       │     │ biết Binance key        │     │ KHÔNG biết Binance key  │
 │ ghi DB (RW)            │     │ đọc + ghi DB            │     │ đọc DB mode=ro          │
 │                        │     │ ghi config.yaml         │     │ 1 route duy nhất        │
 └──────────┬────────────┘     └───────────┬────────────┘     └────────────┬────────────┘
            └──────────────► portfolio_history.db ◄───────────────────────────┘
```

| Plane | Tiến trình | Bind | Bí mật nắm giữ | Quyền với DB |
|---|---|---|---|---|
| Collector | `main.py record` qua cron (`0 */12 * * *`, giờ Europe/Paris) | — (không mở cổng) | Binance key/secret (`config.yaml` / env / `.env`) | INSERT |
| Presentation | `main.py serve` → uvicorn `web_api:app` | **mặc định** `127.0.0.1:8080`; **đang chạy** `0.0.0.0:8088` [đã kiểm chứng] | Binance key/secret, `web_token` (nếu đặt) | INSERT + SELECT, ghi `config.yaml` |
| Gateway | `api_gateway.py` (systemd **user** unit `crypto-api-gateway.service`) | `127.0.0.1:8090` (bắt buộc loopback, code từ chối host khác) | Chỉ `GATEWAY_API_KEY` (`.env.gateway`, mode 0600) | SELECT (`mode=ro` + `PRAGMA query_only`) |

**Vì sao tách gateway khỏi `web_api.py`:** gateway *không import gì* từ dashboard (`config.py`,
`clients/*`, `services/*`). Nếu gateway bị chiếm quyền thì kẻ tấn công cũng không có Binance key để
lấy, không có route nào để ghi config, và không có code nào gọi mạng ra ngoài.

---

## 2. Chi tiết thành phần (Component Deep Dive)

### 2.1 Data Ingestion

#### 2.1.1 `clients/binance.py` — `BinanceClient`

**Transport (`_request`)**

| Cơ chế | Chi tiết |
|---|---|
| Ký request | HMAC-SHA256 trên query string (`timestamp`, `recvWindow` mặc định 5000ms), header `X-MBX-APIKEY`. |
| Host failover | Signed: `api.binance.com` → `api1` → `api2`. Public: `data-api.binance.vision` (trả lời được cả IP bị 451) → `api.binance.com`. |
| Retry | 3 lần/host. `429/418` thì sleep theo `Retry-After` (tối đa 10s). `5xx` thì backoff `2^attempt`. `451` thì sang host kế. `400/401/403` thì raise ngay (retry vô ích). |
| Lệch đồng hồ | Lỗi `-1021` thì `sync_time(force=True)` qua `/api/v3/time` rồi retry một lần. Offset được cache cho cả phiên. |

**Endpoints đang dùng**

| Hàm | Endpoint | Ghi chú |
|---|---|---|
| `spot_balances` | `GET /api/v3/account` | `free + locked` |
| `earn_flexible` | `GET /sapi/v1/simple-earn/flexible/position` | phân trang `current/size=100`, tối đa 100 trang; `detail="flexible"` |
| `earn_locked` | `GET /sapi/v1/simple-earn/locked/position` | `amount` hoặc `totalAmount`; `detail="locked"` |
| `funding_balances` | `POST /sapi/v1/asset/get-funding-asset` | `free+locked+freeze+withdrawing` (POST nhưng chỉ đọc) |
| `prices` | `GET /api/v3/ticker/price` | **một request** lấy toàn bộ ticker, cache trong instance |
| `api_restrictions` | `GET /sapi/v1/account/apiRestrictions` | chỉ dùng cho nút "Test" trong Settings |

**Định giá (`price_usd`)** chạy theo thứ tự:
1. Stablecoin trong `settings.stablecoins` được ghim **$1.00** (USDT, USDC, FDUSD, DAI, TUSD, USDP, PYUSD, USDE).
2. `<ASSET>{USDT,FDUSD,USDC,BUSD,TUSD}`.
3. Cầu nối `<ASSET>{BTC,ETH,BNB} × {BTC,ETH,BNB}USDT`.
4. Nếu cả ba bước đều thất bại thì giá = `0.0` (nghĩa là "không biết"), không đoán.

**Graceful degradation:** `fetch_all` chạy 4 section độc lập. Section nào lỗi thì chỉ thêm một chuỗi
vào `errors` (`"binance funding: HTTP 401 ..."`), các section còn lại vẫn trả về.

#### 2.1.2 `clients/onchain.py` — `OnChainClient`

| Thành phần | Chi tiết |
|---|---|
| RPC transport | JSON-RPC POST, **failover qua danh sách RPC public** của mỗi chain (`config.DEFAULT_CHAINS`, ghi đè được bằng `chains.<name>.rpcs`). `429/503` thì nghỉ 0.5s rồi retry (2 lần/endpoint). Nếu nhận `200` kèm `error` (`-32603`, `-32005`) thì **chuyển endpoint**, không bỏ cuộc. |
| EVM native | `eth_getBalance` ÷ 1e18 |
| ERC-20 | `eth_call` với selector `balanceOf(0x70a08231)`. Nếu config thiếu `symbol/decimals` thì đọc `decimals()` + `symbol()` từ contract (có cache, hỗ trợ cả `bytes32` của MKR/SAI). Decimals ngoài `[0,36]` hoặc không đọc được thì **bỏ token** và ghi lỗi. |
| Solana | `getBalance` ÷ 1e9; `getTokenAccountsByOwner` cho **cả hai** SPL program (Token + Token-2022), encoding `jsonParsed`. |
| Song song | `ThreadPoolExecutor(max_workers=8)`, mỗi job ứng với một cặp **(chain, wallet)**. Các token *trong* một job được đọc **tuần tự**. |
| Định giá | DefiLlama `prices/current/{keys}`, batch 60 key/request. `price_id` có dạng `coingecko:ethereum`, `ethereum:0x…`, `solana:<mint>`. DefiLlama trả `symbol` thật thì thay cho mint bị cắt (`EPjF..Dt1v`). |

> ⚠️ **Đính chính:** code hiện tại **không dùng Multicall3**. Mỗi ERC-20 tốn 1 `eth_call` (hoặc 3 nếu
> phải tự đọc metadata). Với ~5–7 token × 5 chain × 2 ví thì vào khoảng 60–70 RPC call mỗi lần
> refresh. Đây là một ứng viên tối ưu (gom vào một `aggregate3` mỗi chain), xem `ROADMAP_LEDGER.md` §P0.

**Graceful degradation:** lỗi ở một (chain, wallet) chỉ làm mất phần đó, ví dụ
`"arbitrum/main: https://arb1… HTTP 503"`. DefiLlama lỗi thì holding vẫn được giữ với giá $0.

#### 2.1.3 `config.py` — nguồn cấu hình

Thứ tự ưu tiên: **`DEFAULTS` → `config.yaml` → biến môi trường / `.env`**.

- Env override: `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_ENABLED`, `EVM_WALLETS`,
  `SOLANA_WALLETS`, `PORTFOLIO_DB`, `PORTFOLIO_CHARTS_DIR`, `PORTFOLIO_MIN_VALUE`. Riêng
  `PORTFOLIO_WEB_TOKEN` được đọc trực tiếp trong `web_api.auth_token`.
- `_as_address`: khôi phục địa chỉ EVM **không quote** mà YAML 1.1 parse thành `int`.
- `write_raw_config`: ghi **atomic** (`mkstemp` + `os.replace`), mode **0600**, giữ bản cũ ở
  `config.yaml.bak` (0600). Comment YAML sẽ mất sau khi ghi.
- Đường dẫn tương đối (`db_path`, `charts_dir`) được resolve theo **thư mục dự án**, không theo `cwd`,
  nên cron không cần `cd`.

### 2.2 Aggregation Core — `models.py` + `services/portfolio.py`

**Mô hình dữ liệu (`models.py`)**

```
Holding   (symbol, source, quantity, price_usd, detail, price_id)   value_usd = quantity × price_usd
AssetRow  (symbol, by_source{source→qty}, quantity, price_usd, value_usd, pct, details[])
Portfolio (timestamp, holdings[], rows[], totals{source→usd}, errors[], dust_value_usd, dust_assets)
          total_usd = Σ totals ;  ok = not errors
SOURCES = binance_spot | binance_earn | binance_funding | onchain
```

**`aggregate(holdings, min_value)`**, thuật toán:

1. Bỏ holding có `quantity ≤ 0`.
2. **Gom theo `symbol`** (USDC trên Ethereum + USDC trên Solana + USDC ở Binance Spot thành **một dòng**):
   cộng `by_source[source]`, `quantity`, `value_usd`, nối `details` (chain/wallet, `flexible`/`locked`).
3. `totals[source] += value_usd` (per-source, dùng cho cột `snapshots.*_usd`).
4. **Giá trung bình theo giá trị** chỉ trên phần quantity *đã được định giá*:
   $$\bar P_{\text{symbol}} = \frac{\sum_k Q_k P_k}{\sum_{k:\,P_k>0} Q_k}$$
   Nhờ vậy cột Price × Qty khớp với Value, và giá = 0 nếu không phần nào có giá.
5. `pct = value / total × 100`.
6. **Lọc dust:** dòng có `value_usd < min_value` (mặc định $1) bị loại khỏi `rows` và được đếm vào
   `dust_value_usd`/`dust_assets`, nhưng **vẫn nằm trong `holdings` và `totals`**.
7. Sắp xếp theo `-value_usd`, sau đó theo `symbol`.

**`fetch_portfolio(config)`** gọi Binance rồi on-chain (tuần tự giữa hai nhóm, song song bên trong
on-chain). Mỗi nhóm được bọc `try/except Exception`, nên lỗi ở client này không đánh chìm client kia.

**`portfolio_from_db`** dựng lại portfolio từ snapshot mới nhất (không cần mạng). Hạn chế: DB **không
lưu `detail`**, nên `locations` sẽ rỗng.

**`to_dict`** là JSON schema ổn định cho `show --json` và là nền của `/api/portfolio`.

### 2.3 Storage Engine — `services/db.py` (SQLite)

```sql
CREATE TABLE snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp            TEXT    NOT NULL,          -- ISO8601 UTC, giây, "+00:00"
    total_portfolio_usd  REAL    NOT NULL DEFAULT 0,
    binance_spot_usd     REAL    NOT NULL DEFAULT 0,
    binance_earn_usd     REAL    NOT NULL DEFAULT 0,
    binance_funding_usd  REAL    NOT NULL DEFAULT 0,
    onchain_usd          REAL    NOT NULL DEFAULT 0
);
CREATE TABLE snapshot_assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    source       TEXT NOT NULL,
    quantity     REAL NOT NULL DEFAULT 0,
    price_usd    REAL NOT NULL DEFAULT 0,
    value_usd    REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_snapshots_ts    ON snapshots(timestamp);
CREATE INDEX idx_assets_snapshot ON snapshot_assets(snapshot_id);
CREATE INDEX idx_assets_symbol   ON snapshot_assets(symbol);
```

| Đặc tính | Hiện trạng |
|---|---|
| Grain của `snapshot_assets` | **Một dòng cho mỗi `Holding`**, không phải mỗi (symbol, source). Ví dụ snapshot mới nhất có `ETH/onchain` × 3 dòng (Ethereum, Arbitrum, Base) [đã kiểm chứng]. Mọi phân tích phải `GROUP BY symbol[, source]`. |
| Dust | Luôn lưu, vì `record` gọi `fetch_portfolio(min_value=0)`. |
| Tài sản chưa định giá | Lưu với `price_usd=0`. Snapshot mới nhất có 28/50 dòng giá 0 [đã kiểm chứng], chủ yếu là token on-chain không rõ. |
| Nguồn lỗi | **Không lưu** `errors`. Nguồn lỗi được ghi thành `0` trong cột `*_usd` nên không phân biệt được với "thật sự bằng 0". |
| Timestamp | Chuỗi ISO cùng format, nên so sánh chuỗi trong SQLite cho đúng thứ tự thời gian. |
| Kết nối | Mỗi thao tác mở/đóng một kết nối, `timeout=30`, `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL` (P0.4, bật một lần trong `Database.__init__`, lưu trong file). |
| Migration | **Không có** cơ chế migration (`CREATE TABLE IF NOT EXISTS`). README: "xoá file để làm lại". |
| Khối lượng | 16 snapshots, 818 dòng asset, từ 2026-11-22 đến 2026-11-25 [đã kiểm chứng]. |

**API đọc:** `history(limit, days)` (cũ→mới), `latest()`, `snapshot_before(ts)`, `snapshot_after(ts)`,
`assets(snapshot_id)`, `count()`.

### 2.4 Internal Web UI & PWA — `web_api.py` + `static/`

#### 2.4.1 Backend (`web_api.create_app`)

| Endpoint | Gọi mạng? | Mô tả |
|---|---|---|
| `GET /api/portfolio` | ❌ | Trả lần fetch live gần nhất (`app.state.live`, nếu id của nó trùng snapshot mới nhất), nếu không thì snapshot mới nhất, nếu không thì `empty`. Kèm `groups` (all/binance/onchain), `value_by_source`, `binance_*`/`onchain_*` theo từng asset, `change_24h`. |
| `POST /api/portfolio/refresh` | ✅ | `fetcher(cfg)` → `Database.record` → payload. Có `threading.Lock` để hai lần bấm không ghi trùng. Mọi nguồn đều lỗi thì trả **502** và không ghi. |
| `GET /api/portfolio/history?days=N` | ❌ | Tổng của các snapshot, mỗi giây một điểm, dùng cho chart. |
| `GET/POST /api/settings` | ❌ | Credential được **mask** (`abcd****wxyz`, `****wxyz`). POST merge vào `config.yaml` (giữ `chains`, `settings`, `tokens` của từng ví), kiểm tra địa chỉ EVM/Solana và tập chain hợp lệ. |
| `POST /api/settings/test-binance` | ✅ | `apiRestrictions`: cảnh báo nếu key có quyền trade/withdraw/futures hoặc không giới hạn IP. Dịch mã lỗi Binance sang câu dễ hiểu. |
| `POST /api/settings/test-wallet` | ✅ | Đọc native balance song song trên từng chain đã chọn, định giá qua DefiLlama. |
| `POST /api/auth` | ❌ | Đổi token lấy cookie. |
| `GET /api/health`, `/api/docs` | ❌ | Swagger UI nằm **sau** middleware token. |

**`change_since` (biến động 24h):** chọn snapshot tham chiếu là *snapshot mới nhất ≤ now−24h*, nếu
không có thì *snapshot cũ nhất ≥ now−24h*. Kết quả trả về `hours` thực tế để UI ghi "past 6h" thay vì
giả vờ là 24h. **Hệ quả:** con số này là ΔNAV thô, **nạp/rút được tính như lãi/lỗ** (trade-off đã ghi
trong `PRODUCT.md`).

**Cấu hình nóng:** `config()` đọc lại `config.yaml` **ở mỗi request**, nên sau khi lưu Settings
không cần restart.

**Auth (`require_token` middleware):**
- Token = `PORTFOLIO_WEB_TOKEN` (env) hoặc `settings.web_token`.
- Áp dụng cho mọi `/api/*` trừ `/api/auth`. **App shell (`/`, `/static/*`) luôn public**: shell
  public, dữ liệu private.
- Chấp nhận `Authorization: Bearer <t>`, `X-Portfolio-Token: <t>`, hoặc cookie `portfolio_token`
  (HttpOnly, SameSite=Strict, `Secure` khi chạy https, sống 365 ngày).
- `/?token=<t>`: đặt cookie rồi **303 redirect về `/`** để token không nằm lại trên URL/lịch sử.
- So sánh bằng `secrets.compare_digest`.
- **Không có token thì API mở hoàn toàn.** `main.py serve` in cảnh báo khi bind khác loopback
  mà không có token.

#### 2.4.2 Frontend (`static/index.html`, `static/app.js`, `static/style.css`)

| Hạng mục | Chi tiết |
|---|---|
| Stack | **Vue 3 global build**, **Tailwind CSS browser build** (JIT trong trình duyệt), **TradingView Lightweight Charts v5**, font Manrope. Tất cả nằm trong `static/vendor/` và `static/fonts/`, **không CDN**. |
| Hero | Net worth được tách phần nguyên/phần cent. Chip biến động 24h có **dấu + mũi tên + màu** (theo yêu cầu accessibility). Trong lúc scrub chart, hero hiển thị giá trị tại điểm scrub và Δ so với điểm đầu range. |
| Chart | `AreaSeries`, màu theo xu hướng của range (xanh `#22E58B` / đỏ `#FF5C73` / xám). Range 24H/7D/1M/1Y/ALL, cache theo `days`. **Scrub cảm ứng** có logic riêng: giữ 130ms hoặc kéo ngang 8px mới bắt đầu scrub, vuốt dọc thì nhường cho cuộn trang. Có pointer capture. |
| **Gom nhóm tài sản** | Tab **All / Binance / On-chain** (`visibleAssets`). Mỗi tab tính lại quantity, value và % **trong nhóm** (`binance_quantity`, `onchain_value_usd`…). Chạm vào một asset để xem **breakdown theo source** (Spot/Earn/Funding/On-chain) và **locations** (chain/wallet). Dòng dust bị ẩn ở tab con, kèm dòng chú thích "N balances under $1 … counted in the total". |
| **Privacy mode** | Nút mắt bật/tắt class `privacy` trên `<body>`. Mọi phần tử `.priv` bị `filter: blur(10px)` (`.priv-soft` là 6px). Trạng thái lưu ở `localStorage['portfolio.privacy']`. |
| Trạng thái lỗi | Banner "N sources failed, so the total may be incomplete" (mở ra xem danh sách lỗi). Toast khi refresh. Màn hình **unlock** khi API trả 401. |
| Độ tươi dữ liệu | Khi mở app, hoặc khi quay lại tab (`visibilitychange`), nếu dữ liệu cũ hơn **10 phút** thì tự refresh ngầm. Mỗi lần refresh tạo **một snapshot**. |
| Settings sheet | Form Binance key: kiểm tra trực tiếp 64 ký tự, phát hiện khoảng trắng, nút Test hiển thị chip quyền Read/Trade/Withdraw/Futures/IP-locked. Danh sách ví: tự nhận diện EVM/Solana, chọn chain, test số dư, xoá có xác nhận 2 chạm, cảnh báo khi rời form chưa lưu, focus trap, Esc. |
| PWA | `manifest.json` (standalone, portrait, maskable icon). `sw.js`: shell **network-first**, `/static/vendor|fonts|icons|coins` **cache-first**, `/api/*` **không bao giờ cache** ("net worth cũ là nói dối"). |

> ⚠️ Service worker chỉ đăng ký khi `window.isSecureContext`. Truy cập qua `http://192.168.1.66:8088`
> thì **không có SW**, tức không có offline shell. "Add to Home Screen" vẫn hoạt động, nhưng offline
> chỉ chạy được qua HTTPS hoặc `localhost`.

### 2.5 Public API Gateway — `api_gateway.py`

**Hợp đồng công khai (một route duy nhất)**

```
GET https://api.tytony.link/v1/crypto/portfolio/total?currency=USD
X-API-Key: <GATEWAY_API_KEY>

200 {
  "value": "12345.67",                 // chuỗi Decimal, ROUND_HALF_UP 2 chữ số
  "currency": "USD",
  "as_of": "2026-11-25T00:44:42Z",
  "stale": true,                       // now - as_of > GATEWAY_STALE_AFTER_SECONDS (1800)
  "sources": [ {"id":"binance-spot","status":"ok","value":"…"}, … ]   // status ∈ ok|empty
}
400 {"error":"unsupported_currency","supported":["USD"]} | {"error":"invalid_request"}
429 {"error":"rate_limited"}  + Retry-After
503 {"error":"no_data"} | {"error":"data_unavailable"}
404 {"detail":"Not Found"}            // MỌI trường hợp không hợp lệ khác
```

**Lớp phòng thủ (theo thứ tự trong middleware `gatekeeper`)**

| Lớp | Cơ chế |
|---|---|
| Ngụy trang 404 | Request bị từ chối nếu thiếu **bất kỳ** điều kiện nào trong: `X-API-Key` đúng ∧ method `GET` ∧ path đúng ∧ `Host` ∈ allowlist. Mọi trường hợp đều nhận **cùng** `404 {"detail":"Not Found"}` như một URL không tồn tại. `405` và `422` (validation) **không lộ ra trước khi xác thực**. Không có `/docs`, `/openapi.json`. Uvicorn tắt header `Server`. |
| So khớp key | `secrets.compare_digest` trên bytes latin-1, **luôn được đánh giá** (không early-exit) để giảm side-channel về thời gian. |
| Rate limit | `SlidingWindowLimiter`: **30 req/phút/client**, khoá theo `CF-Connecting-IP` (tin được vì gateway chỉ bind loopback và client duy nhất là cloudflared). Bộ nhớ có giới hạn (10k client, evict client idle, flood thì clear). Request trái phép **cũng tiêu quota**, nên dò key sẽ tự bị chặn. |
| DB read-only | `sqlite3.connect("file:…?mode=ro", uri=True)` + `PRAGMA query_only=ON`. File thiếu thì báo lỗi (`mode=ro` không bao giờ tạo file), gateway trả 503. |
| Headers | `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. |
| Cấu hình | `.env.gateway`: tự sinh lần đầu với key **256-bit** (`token_urlsafe(32)`), mode 0600 (tự sửa nếu bị nới quyền). Env `GATEWAY_*` thắng file. Key < 32 ký tự hoặc `GATEWAY_HOST` không phải loopback thì **từ chối khởi động**. `api_gateway.py show-key` in key ra. |
| systemd sandbox | `crypto-api-gateway.service` (user unit): `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=<project>`, `PrivateTmp`, `UMask=0077`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `Restart=on-failure`. |
| Cloudflare | `cloudflared.service` là **system service**. Không có `config.yml` cục bộ (`~/.cloudflared` chỉ có `cert.pem`, `/etc/cloudflared` rỗng) nên **[suy luận]** đây là tunnel *remotely-managed* (chạy bằng token), ingress `api.tytony.link → http://127.0.0.1:8090` được khai báo trên Cloudflare dashboard. Có `cloudflared-update.timer` chạy hằng ngày. |

**Test:** `test_api_gateway.py` gồm 19 test: 404 đồng nhất, method/host sai, 422 không lộ, stale flag,
timestamp naive, DB rỗng hoặc thiếu (và không bị tạo ra), kết nối thật sự read-only, 429 vs 404,
limiter có giới hạn, quyền file env, kiểm tra loopback, key ngắn.

### 2.6 CLI & Visualizer — `main.py` + `services/visualizer.py`

| Lệnh | Hành vi | Exit code |
|---|---|---|
| `show [--json] [--text-charts]` | fetch live, in bảng cố định cột (Spot/Earn/Funding/On-chain/Qty/Price/Value/%) hoặc JSON | 0 ok · 2 lỗi một phần |
| `record [-v]` | fetch với `min_value=0`, ghi snapshot. **Im lặng khi thành công**. Lỗi ra stderr (cron sẽ mail). Mọi nguồn lỗi thì **từ chối ghi** `$0`. | 0 · 1 · 2 |
| `chart --type all\|history\|allocation\|sources\|text [--from-db]` | PNG vào `./charts` hoặc báo cáo ASCII | 0 · 1 |
| `history [--days] [--limit] [--json]` | bảng snapshot kèm Δ và sparkline | 0 |
| `serve [--host] [--port]` | uvicorn `web_api`, `proxy_headers=True`, `forwarded_allow_ips=127.0.0.1` | — |

**Visualizer** (Matplotlib, backend `Agg` headless):
- `history_trend.png`: **stacked area theo source** (các phần cộng lại bằng tổng, baseline 0). 0 điểm
  thì vẽ empty state; 1 điểm thì vẽ "hero figure" (không vẽ biểu đồ line một điểm).
- `token_allocation.png`: donut **tối đa 6 lát**, phần đuôi gộp thành "Others" (xám trung tính).
- `source_distribution.png`: bar ngang (vì đây là so sánh, không phải chia phần).
- Palette categorical đã được kiểm định cho người mù màu. **Màu gắn với thực thể, không gắn với thứ
  hạng**, nên sắp xếp lại không đổi màu.
- ASCII: `sparkline`, `bar`, `text_report`.

### 2.7 Kiểm thử

`pytest -q` → **147 passed trong 7.3s** [đã kiểm chứng]. Toàn bộ HTTP được mock nên chạy offline.

| File | Số test | Phạm vi |
|---|---|---|
| `test_all.py` | 58 hàm (một số tham số hoá) | clients, aggregate, db, config, CLI, visualizer |
| `test_api.py` | 49 hàm | FastAPI TestClient, config/DB tạm, auth, settings |
| `test_api_gateway.py` | 19 hàm | xem §2.5 |

---

## 3. Sơ đồ luồng dữ liệu

### 3.1 Kiến trúc tổng thể & ranh giới mạng

```
                                    PUBLIC INTERNET
   ┌───────────────┐  HTTPS + X-API-Key  ┌──────────────────────────────────┐
   │ API consumer  │ ──────────────────► │ Cloudflare edge                  │
   │ (bot / agent) │ ◄────────────────── │ api.tytony.link  (TLS, DDoS, WAF)│
   └───────────────┘                     └────────────────┬─────────────────┘
                                                          │ tunnel outbound-only
                                                          │ (không mở cổng inbound nào)
 ═════════════════════════════════════════════════════════╪══════ HOST BOUNDARY (Debian, Europe/Paris)
                                                          ▼
                                            cloudflared.service (system)
                                                          │ http://127.0.0.1:8090
 ┌──────────────── LOOPBACK ONLY ─────────────────────────▼───────────────────────────┐
 │  api_gateway.py  (systemd --user, sandboxed)                                        │
 │   gatekeeper: key ∧ GET ∧ path ∧ Host ──no──► 404 "Not Found"                       │
 │              rate limit 30/min/CF-Connecting-IP ──► 429                             │
 │   secrets: .env.gateway (0600) — KHÔNG có Binance key, KHÔNG import config.py       │
 └────────────────────────────────────┬────────────────────────────────────────────────┘
                                      │ SELECT … (file:…?mode=ro, PRAGMA query_only)
                                      ▼
                     ╔══════════════════════════════════════╗
                     ║ portfolio_history.db  (SQLite, 0644)  ║
                     ║  snapshots ─1:N─ snapshot_assets      ║
                     ╚═════════▲════════════════════▲═══════╝
                       INSERT  │                    │ INSERT / SELECT
          ┌────────────────────┘                    └───────────────────────────┐
          │                                                                     │
 ┌────────┴──────────────────┐                          ┌──────────────────────┴───────────┐
 │ COLLECTOR                 │                          │ PRESENTATION  web_api.py          │
 │ cron  0 */12 * * *        │                          │ uvicorn  0.0.0.0:8088 (LAN) ⚠     │
 │ main.py record            │                          │  GET  /api/portfolio  (no network)│
 └────────┬──────────────────┘                          │  POST /api/portfolio/refresh ─┐   │
          │                                             │  /api/settings ⇄ config.yaml  │   │
          │                                             │  /static  (PWA shell, public) │   │
          │                                             └───────────▲───────────────────┼───┘
          │                                                         │ HTTP (LAN)        │
          │                                             ┌───────────┴─────────┐         │
          │                                             │ 📱 PWA (Vue 3, LWC) │         │
          │                                             │ privacy blur, tabs  │         │
          │                                             └─────────────────────┘         │
          ▼                                                                             ▼
 ┌───────────────────────────── services/portfolio.fetch_portfolio(config) ──────────────────────┐
 │  config.py: DEFAULTS → config.yaml (0600) → env/.env      ← Binance key/secret sống ở đây      │
 │                                                                                                │
 │  clients/binance.py ── HMAC GET/POST ──► api.binance.com / api1 / api2      (signed, read-only)│
 │                     ── GET ticker   ──► data-api.binance.vision             (public)           │
 │  clients/onchain.py ── JSON-RPC ──────► public RPCs × {ETH,BSC,ARB,BASE,POL,SOL} (failover)    │
 │                     ── GET prices ────► coins.llama.fi                      (public, keyless)  │
 │                                                                                                │
 │  aggregate(): group by symbol → avg price on priced qty → totals per source → dust filter      │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Luồng ghi (write path), một snapshot

```
trigger (cron | POST /refresh)
  └─► load_config()  ──► Binance.fetch_all()          ──► holdings_B, errors_B
                     └─► OnChain.fetch_all() [8 thread] ──► holdings_O, errors_O
  └─► aggregate(holdings_B ∪ holdings_O, min_value)
  └─► if not holdings and errors: ABORT (cron exit 1 | HTTP 502)
  └─► Database.record():  BEGIN
          INSERT snapshots(timestamp, total, spot, earn, funding, onchain)
          INSERT snapshot_assets × len(holdings)      -- 1 dòng / Holding, kể cả dust & giá 0
      COMMIT
```

### 3.3 Luồng đọc (read path)

| Consumer | Đọc gì | Tần suất |
|---|---|---|
| PWA `GET /api/portfolio` | `app.state.live` hoặc `latest()` + `assets()` + `snapshot_before/after` (24h) | mỗi lần mở app |
| PWA `GET /api/portfolio/history` | `snapshots` theo `days` | mỗi lần đổi range |
| Gateway | `snapshots` dòng mới nhất (6 cột) | theo consumer, ≤ 30/phút/IP |
| `main.py chart --from-db`, `history` | `snapshots`, `snapshot_assets` | thủ công |

---

## 4. An ninh & bảo mật

### 4.1 Phân cấp bí mật (secret tiering)

| Tier | Bí mật | Nơi lưu | Ai đọc | Ảnh hưởng nếu lộ |
|---|---|---|---|---|
| **T1 — Credential sàn** | Binance API key + secret | `config.yaml` (0600) **hoặc** env/`.env` (git-ignored) | `main.py` (cron), `web_api.py` | Đọc toàn bộ số dư Binance. **Không** trade hay rút được nếu key chỉ có *Enable Reading* (Settings → Test sẽ xác nhận). |
| **T2 — Token truy cập dashboard** | `PORTFOLIO_WEB_TOKEN` / `settings.web_token` | env hoặc `config.yaml` | `web_api.py` | Đọc số dư, **ghi `config.yaml`**: thay Binance key, thêm/xoá ví. |
| **T3 — Key gateway public** | `GATEWAY_API_KEY` (256-bit) | `.env.gateway` (0600, git-ignored) | `api_gateway.py` | Chỉ đọc **tổng + 4 tổng theo source** của snapshot mới nhất. |
| T4 — Dữ liệu nhạy cảm (không phải secret) | Địa chỉ ví, lịch sử số dư | `config.yaml`, `portfolio_history.db` (**0644**) | mọi plane | Lộ quy mô tài sản và địa chỉ đang theo dõi. |

**Bất biến kiến trúc:** gateway (T3) **không thể** leo lên T1/T2. Nó không import `config.py`, không
đọc `config.yaml`, không có route ghi, và kết nối DB ở chế độ `query_only`. Lộ T3 chỉ lộ đúng một con
số. Còn T1 dù lộ cũng không cho phép di chuyển tiền (với điều kiện key chỉ có quyền đọc).

### 4.2 Ranh giới mạng

| Ranh giới | Thành phần | Trạng thái |
|---|---|---|
| **Cloudflare edge** | TLS termination, chống DDoS volumetric, (tuỳ chọn) WAF/Rate Limiting rules | Bên ngoài host. Tunnel là kết nối **outbound**, router không mở cổng nào. |
| **Loopback 127.0.0.1** | `api_gateway.py :8090` | Code **cưỡng chế** loopback (`_is_loopback`), không cấu hình sai được. |
| **LAN 0.0.0.0** | `web_api.py :8088` | ⚠ Đang lộ ra **mọi interface**: `192.168.1.66` và các bridge Docker `172.17/18.0.1` [đã kiểm chứng]. |
| **Egress** | Binance, RPC public, DefiLlama | Chỉ truy vấn số dư/giá. RPC public **thấy địa chỉ ví và IP của máy** (rủi ro privacy vốn có của việc dùng RPC công cộng). |

### 4.3 Kiểm soát đã có

- Binance: chỉ gọi endpoint đọc, nút Test cảnh báo quyền nguy hiểm và key không giới hạn IP.
- Settings: không bao giờ echo secret về trình duyệt, chỉ trả dạng mask. Giá trị đã mask gửi ngược
  lên thì được hiểu là "giữ nguyên".
- Ghi config atomic, 0600, có `.bak`.
- Cookie HttpOnly + SameSite=Strict, nên JS (kể cả XSS) không đọc được token và CSRF cross-site bị chặn.
- Gateway: xem §2.5.

### 4.4 Lỗ hổng & khuyến nghị (xếp theo mức độ)

| ID | Mức | Phát hiện | Khuyến nghị |
|---|---|---|---|
| **SEC-1** | 🔴 **P0** | `web_api` chạy `0.0.0.0:8088` **không có `web_token`**: `GET /api/settings` và `/api/portfolio` trả **200** khi không có token [đã kiểm chứng]. Bất kỳ thiết bị nào trong LAN (hoặc container Docker) đều đọc được số dư và có thể `POST /api/settings` để **thay Binance key / danh sách ví** (đầu độc dữ liệu). | Đặt `PORTFOLIO_WEB_TOKEN` (≥ 32 byte ngẫu nhiên) ngay. Bind `192.168.1.66` thay vì `0.0.0.0` để Docker bridge không chạm tới. Lâu dài: cho `serve` **từ chối** bind non-loopback khi không có token (hiện chỉ cảnh báo). |
| SEC-2 | 🟠 P1 | Tiến trình 8088 được khởi động **ad-hoc** qua `systemd-run` scope (từ một agent), không phải unit bền vững: reboot là mất dashboard, và không có sandbox như gateway. | Tạo `deploy/crypto-web.service` (user unit), sandbox tương đương gateway, `EnvironmentFile=` chứa token. |
| SEC-3 | 🟠 P1 | `api_gateway.py`, `deploy/`, `test_api_gateway.py` **chưa được commit** (untracked). | Commit (không commit `.env.gateway`, vốn đã nằm trong `.gitignore`). |
| SEC-4 | 🟡 P2 | `portfolio_history.db` mode **0644**: user khác trên host đọc được lịch sử tài sản. | `chmod 600`. `Database.__init__` nên tạo file với `umask 077`. |
| SEC-5 | 🟡 P2 | Dashboard chạy HTTP thuần trên LAN: token/cookie đi plaintext, và SW không đăng ký được. | Đưa dashboard vào Cloudflare Tunnel thứ hai + **Cloudflare Access** (SSO/OTP), hoặc dùng Tailscale/WireGuard. Cookie sẽ tự bật `Secure` khi chạy https. |
| SEC-6 | 🟢 P3 | `/api/docs` (Swagger) có tồn tại dù nằm sau token. | Tắt khi bind non-loopback. |
| SEC-7 | 🟢 P3 | RPC public thấy địa chỉ ví + IP nhà. | Dùng RPC riêng (Alchemy/Infura) qua `chains.<name>.rpcs`, hoặc đi qua proxy. |

---

## 5. Vận hành & triển khai

| Hạng mục | Hiện trạng [đã kiểm chứng] |
|---|---|
| Host | Debian, kernel 6.12, TZ **Europe/Paris** |
| Python | `.venv/` (FastAPI ~0.165, uvicorn ~0.54, requests, PyYAML, matplotlib ⇒ có numpy 2.x). **Không có** pandas/scipy. |
| Collector | crontab user: `0 */12 * * * …/main.py record`, tức 00:00 và 12:00 giờ Paris |
| Dashboard | `main.py serve --host 0.0.0.0 --port 8088` (transient scope). Cổng `127.0.0.1:8080` đang bị một tiến trình khác chiếm (không thuộc dự án này). |
| Gateway | `systemctl --user status crypto-api-gateway` → active |
| Tunnel | `cloudflared.service` (system) active + `cloudflared-update.timer` |
| Backup | Bản sao thủ công `portfolio_history.db.bak_20261125_012104`. Chưa có lịch backup. |

**Hệ quả vận hành đáng chú ý**
- Cron 12h/lần trong khi gateway có `stale_after=30m`, nên consumer nhận `stale=true` khoảng **96%**
  thời gian (30/720 phút). Cần chọn: tăng tần suất `record` (khuyến nghị 1h/lần, đồng thời cũng là
  điều kiện tiên quyết cho analytics) hoặc nâng `GATEWAY_STALE_AFTER_SECONDS` lên ~13h.
- Snapshot ghi lúc một nguồn bị lỗi sẽ tạo **"hố" giả** trong lịch sử. Gateway báo nguồn đó là
  `empty` mà không phân biệt được với lỗi (code đã tự ghi chú điều này).

---

## 6. Kết quả thẩm định: rủi ro & sai lệch so với mô tả

| # | Mô tả trong brief / docs | Thực tế trong code / môi trường | Tác động |
|---|---|---|---|
| A1 | "EVM multicall/RPC" | **Không có Multicall**. `eth_call` từng token, song song theo (chain, wallet). | Hiệu năng / rate limit RPC public |
| A2 | Web PWA "port 8088" | Mặc định là **8080** (`main.py`, README). 8088 chỉ là tham số lúc chạy tay. | Tài liệu / unit cần thống nhất |
| A3 | "Binance key chỉ nằm trong config.yaml/web_api" | Đúng về nơi lưu, nhưng **`main.py record` (cron)** cũng nạp key. Collector là plane thứ hai nắm T1. | Mô hình đe doạ |
| A4 | 24h change | ΔNAV thô, **tính cả nạp/rút**. Tham chiếu là snapshot gần mốc −24h, có thể chỉ 6h nếu lịch sử ngắn. | Cần Attribution + Cash flows (Roadmap F1, F2) |
| A5 | Lịch sử chính xác | Nguồn lỗi được ghi thành **0**, và `errors` không được lưu. | Làm sai MDD, volatility, attribution, cần sửa trước khi làm analytics |
| A6 | Phân tích theo ví / chain | `snapshot_assets` **mất `detail`** (chain/wallet, flexible/locked) | Không làm được on-chain flow detection theo ví từ lịch sử |
| A7 | Định giá đầy đủ | 28/50 dòng của snapshot mới nhất có giá 0 (token lạ hoặc spam) | Nhiễu danh sách, nhưng giá trị bằng 0 nên không ảnh hưởng tổng |
| A8 | Schema tiến hoá được | Không có migration (`PRAGMA user_version` chưa dùng) | Bắt buộc phải có trước khi thêm `cash_flows` |
| A9 | Đồng thời ghi/đọc | ~~Journal mode rollback, gateway có `timeout=5`~~ Đã xử lý ở P0.4: WAL | Gateway `mode=ro` đọc snapshot đã commit trong lúc collector ghi, không còn `database is locked`. |

---

## 7. Phụ lục: hợp đồng dữ liệu

### 7.1 `GET /api/portfolio` (rút gọn)

```jsonc
{
  "timestamp": "2026-11-25T00:44:42+00:00",
  "total_usd": 12345.67,
  "sources": {"binance_spot": 0, "binance_earn": 0, "binance_funding": 0, "onchain": 0},
  "assets": [{
    "symbol": "ETH", "quantity": 1.23, "price_usd": 3500.0, "value_usd": 4305.0, "pct": 34.87,
    "by_source": {"binance_earn": 0.5, "onchain": 0.73},
    "value_by_source": {"binance_earn": 1750.0, "onchain": 2555.0},
    "binance_quantity": 0.5, "binance_value_usd": 1750.0,
    "onchain_quantity": 0.73, "onchain_value_usd": 2555.0,
    "locations": ["flexible", "ethereum/main", "arbitrum/main"]
  }],
  "dust": {"assets": 3, "value_usd": 1.12},
  "errors": [], "ok": true,
  "origin": "live|snapshot|empty", "snapshot_id": 16, "configured": true, "min_value": 1.0,
  "groups": {"all": 12345.67, "binance": 9000.0, "onchain": 3345.67},
  "source_labels": {"binance_spot": "Binance Spot", "…": "…"},
  "change_24h": {"usd": 120.5, "pct": 0.98, "since": "…", "hours": 23.9, "reference_usd": 12225.17}
}
```

### 7.2 `GET /api/portfolio/history?days=30`

```jsonc
{"days": 30, "count": 16, "points": [
  {"time": 1764037482, "timestamp": "…", "total_usd": 0, "binance_usd": 0, "onchain_usd": 0,
   "binance_spot_usd": 0, "binance_earn_usd": 0, "binance_funding_usd": 0}
]}
```

### 7.3 Bản đồ file

| Path | Vai trò |
|---|---|
| `main.py` | CLI: `show`, `record`, `chart`, `history`, `serve` |
| `config.py` | defaults → YAML → env; ghi config atomic |
| `models.py` | `Holding`, `AssetRow`, `Portfolio`, hằng `SOURCES` |
| `clients/binance.py` | Binance read-only + giá ticker |
| `clients/onchain.py` | EVM/Solana JSON-RPC + DefiLlama |
| `services/portfolio.py` | orchestration + `aggregate` + `to_dict` |
| `services/db.py` | schema SQLite + đọc/ghi snapshot |
| `services/visualizer.py` | PNG Matplotlib + báo cáo ASCII |
| `web_api.py` | FastAPI dashboard + host PWA |
| `api_gateway.py` | gateway public read-only |
| `static/` | `index.html`, `app.js`, `style.css`, `sw.js`, `manifest.json`, `vendor/`, `fonts/`, `icons/`, `coins/` |
| `deploy/crypto-api-gateway.service` | systemd user unit cho gateway |
| `test_all.py`, `test_api.py`, `test_api_gateway.py` | 147 test, offline |
