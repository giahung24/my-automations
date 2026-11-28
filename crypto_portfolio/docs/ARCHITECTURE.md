# ARCHITECTURE — `crypto_portfolio`

> Hệ thống đang chạy thế nào. Viết lại ngày 2026-11-27 theo code hiện tại, cập nhật 2026-11-28 sau
> Phase 3 (schema v11); bản thẩm định chi tiết
> ngày 2026-11-25 nằm trong lịch sử git (`SYSTEM_ARCHITECTURE.md`), các phát hiện của nó được đối
> chiếu ở [`LEDGER.md`](LEDGER.md) §4. Khi tài liệu này và code lệch nhau, code là chuẩn.

## 1. Sản phẩm và nguyên tắc

`crypto_portfolio` gom mọi tài sản crypto của một người về một con số Net Worth (USD), kèm lịch sử,
giải thích biến động và gợi ý phân bổ.

| Nguyên tắc | Cách code bảo đảm |
|---|---|
| **Chỉ đọc** | Client Binance chỉ có hàm đọc; key chỉ cần *Enable Reading*, và Settings cảnh báo khi key có quyền trade hay rút. On-chain chỉ dùng địa chỉ công khai, không ký giao dịch. |
| **Local-first** | Dữ liệu nằm trong SQLite trên máy. Chỉ truy vấn số dư và giá đi ra ngoài. Không telemetry, không CDN: Vue, Tailwind, Lightweight Charts và font đều nằm trong `static/vendor/`. |
| **Không sai im lặng** | Lỗi từng nguồn được ghi lại và báo cạnh con số. Dust ẩn khỏi danh sách nhưng vẫn tính vào tổng và vẫn lưu. Token không đọc được `decimals()` thì bỏ qua; tài sản không có giá giữ $0. |
| **`GET` không gọi mạng** | Mọi endpoint đọc phục vụ từ SQLite. Chỉ `POST /api/portfolio/refresh` và hai nút Test trong Settings gọi Binance hay RPC. |

## 2. Ba tiến trình, một file SQLite

```
 COLLECTOR                    DASHBOARD                       GATEWAY PUBLIC
 main.py record (cron 1h)     main.py serve → web_api.py      api_gateway.py
 biết Binance key             biết Binance key + token        chỉ biết GATEWAY_API_KEY
 ghi DB                       đọc + ghi DB, ghi config.yaml   đọc DB (mode=ro), 1 route
        └──────────────► portfolio_history.db ◄──────────────────────┘
```

| Tiến trình | Chạy bằng | Bind | Bí mật | Quyền DB |
|---|---|---|---|---|
| Collector | crontab `0 * * * *` | không mở cổng | Binance key | ghi |
| Dashboard | `deploy/crypto-web.service` (systemd user) | `0.0.0.0:8088`, bắt buộc có token | Binance key, `PORTFOLIO_WEB_TOKEN` | đọc + ghi |
| Gateway | `deploy/crypto-api-gateway.service` | `127.0.0.1:8090`, sau Cloudflare Tunnel | `GATEWAY_API_KEY` | chỉ đọc |

Gateway không import gì từ phần còn lại (`config.py`, `clients/`, `services/`): bị chiếm quyền thì
cũng không có Binance key để lấy, không có route ghi, không có code gọi mạng ra ngoài.

## 3. Thành phần

### 3.1 Thu thập — `clients/`, `config.py`

**`clients/binance.py`**
- Transport: ký HMAC-SHA256, failover host (`api` → `api1` → `api2`; dữ liệu công khai qua
  `data-api.binance.vision`), retry có backoff, tự đồng bộ đồng hồ khi gặp lỗi `-1021`.
- Số dư: Spot, Simple Earn flexible + locked, Funding. Mỗi phần lỗi độc lập.
- APR niêm yết của Earn flexible: `listed_apr` = base `latestAnnualPercentageRate` + bonus
  `tierAnnualPercentageRate` (band dạng `{"0-5000USDC": "0.03"}`) tính theo phần số dư nằm trong
  từng band (`parse_tiers`, `effective_apr`), tức lãi suất cả vị thế thực hưởng. Giữ phần tách
  `listed_base_apr`, `listed_tiers`.
- Lịch sử (`HistoryStream`, cho dòng tiền): nạp/rút crypto, lệnh fiat, thanh toán fiat, P2P, lãi Earn
  flexible + locked.
- Giá: stablecoin ghim $1.00 → cặp `<ASSET>USDT/FDUSD/USDC…` → cầu nối qua BTC/ETH/BNB → không có thì 0.

**`services/peg.py`** (N3) — vì stablecoin bị ghim $1.00, mỗi `record` ghi thêm giá thật của từng
stablecoin đang giữ theo hai tham chiếu: ticker `<SYM>USDT` đã tải và giá USD của DefiLlama. Tham
chiếu lỗi được ghi kèm `error`, không chặn snapshot.

**`clients/onchain.py`**
- EVM (Ethereum, BSC, Arbitrum, Base, Polygon): coin gốc + các ERC-20 đã cấu hình, một `eth_call` mỗi
  token (chưa có Multicall, P0.7). Solana: SOL + mọi SPL token (cả Token-2022).
- Mỗi chain có danh sách RPC công khai để failover; ghi đè bằng `chains.<name>.rpcs`.
- Song song theo cặp (chain, ví). Giá từ DefiLlama (`coins.llama.fi`), không cần key.

**`config.py`** — thứ tự ưu tiên: mặc định → `config.yaml` → biến môi trường / `.env`. Các block:
`binance`, `wallets`, `chains`, `settings`, `cash_flows`, `manual_assets`, `allocation`. Ghi config
là atomic, mode 0600, giữ bản `.bak`; comment YAML mất sau khi ghi từ UI.

### 3.2 Tổng hợp — `models.py`, `services/portfolio.py`, `services/manual.py`

- `Holding` (symbol, source, quantity, price, detail, price_id; với Earn thêm `listed_apr`,
  `listed_base_apr`, `listed_tiers`) → `aggregate()` gom theo symbol, tính
  giá trung bình theo giá trị, tổng theo nguồn, lọc dust.
- Năm nguồn: `binance_spot`, `binance_earn`, `binance_funding`, `onchain`, `manual`.
- **Tài sản manual** (`manual_assets` trong config): ví lạnh, staking ngoài, sàn khác. Định giá qua
  cùng pipeline (Binance → DefiLlama → `price_usd` cố định). Đổi số lượng được ghi thành dòng tiền,
  không tính là lãi. `linked_wallet` gộp hiển thị với một ví on-chain đã cấu hình.

### 3.3 Lưu trữ — `services/db.py`

SQLite ở chế độ WAL, file mode 0600. Schema có phiên bản (`PRAGMA user_version`, hiện **v11**);
migration chạy trong một transaction và sao lưu file trước. Phase 3 thêm v6–v11: `listed_apr` và
`peg_checks`, phần tách APR theo tier, `trades`, sáu view phân tích (v9, viết lại ở v10), `notes`.
Đo trên DB giả 10 năm (87,601 snapshot, 441 MB): các màn hình hằng ngày vẫn ≤ 0.1 giây.

| Bảng | Nội dung |
|---|---|
| `snapshots` | Một dòng mỗi lần ghi: tổng, năm tổng theo nguồn, `trigger` (cron / refresh), `is_backfilled` |
| `snapshot_assets` | Một dòng mỗi `Holding`: symbol, source, số lượng, giá, `detail` (chain/ví, flexible/locked), `price_id`; với Earn: `listed_apr`, `listed_base_apr`, `listed_tiers` (JSON `[{"min", "max", "apr"}]`) |
| `snapshot_sources` | Trạng thái từng nguồn của mỗi snapshot (ok / error / disabled) |
| `peg_checks` | Giá thật của mỗi stablecoin đang giữ theo từng tham chiếu (`binance:…`, `defillama:…`), `error` nếu lỗi; xoá snapshot thì xoá theo |
| `cash_flows` | Nạp, rút, fiat, P2P, lãi Earn (giữ `earn_backfill_days` = 1827 ngày), thay đổi tài sản manual; cờ chuyển khoản nội bộ và cặp ghép |
| `trades` | Mỗi fill Spot và mỗi lần Convert trên Binance: cặp, `side` theo `base`, số lượng, giá trị USD, cờ `estimated` khi quote không phải stablecoin |
| `notes` | Nhật ký: một ghi chú văn bản mỗi ngày. Dữ liệu duy nhất không tải lại được từ API nào |
| `sync_state` | Con trỏ đồng bộ tăng dần của từng luồng lịch sử Binance (kể cả id fill cuối của từng cặp) |
| view `v_*` | `v_nav_daily`, `v_positions_daily`, `v_external_flows`, `v_daily_returns`, `v_earn_income_daily`, `v_trades`: lớp đọc cho notebook và `main.py export` |

`services/backfill.py` dựng 60 checkpoint tháng (2021-11 đến 2026-10) từ giá lịch sử, gắn
`is_backfilled = 1`: số dư hôm nay nhân với giá của tháng đó.

### 3.4 Phân tích — `services/analytics/`, `services/cash_flows.py`, `services/activity.py`

| Module | Trả lời câu hỏi | Endpoint |
|---|---|---|
| `attribution.py` | Vì sao NAV đổi: giá, số lượng, tương tác, định giá lại, thiếu dữ liệu | `GET /api/portfolio/attribution` |
| `pnl.py` | Lãi/lỗ sau khi trừ nạp/rút, theo cửa sổ; vốn đã nạp ròng luỹ kế cho từng điểm của chart (N8) | `GET /api/portfolio/pnl`, `GET /api/portfolio/history` |
| `quant.py` | TWR, Base 100 so với BTC/ETH, drawdown, volatility | `GET /api/analytics/quant` |
| `allocation.py` | Lệch mục tiêu bao nhiêu, cần mua/bán gì (`target`, `band_edge`, `contribution`, góp vốn một phần) | `GET /api/analytics/allocation` |
| `earn.py` (N1) | Mỗi tài sản Earn: APR niêm yết, APR thực nhận 7d/30d (`earned / mean_balance × 365 / span_days`, kèm `n_obs`, `sufficient`; không đủ ≥ 90% cửa sổ hoặc có hố > 6 giờ thì `None`), $/ngày, thu nhập luỹ kế theo năm | `GET /api/analytics/earn` |
| `cash_flows.py` | Đồng bộ lịch sử Binance, ghép chuyển khoản nội bộ (địa chỉ → `tx_hash` → heuristic) | chạy sau mỗi `record` |
| `reconcile.py` (N2, N4) | Phần dư số lượng Binance giữa hai snapshot sau khi trừ flow và trade thật → trade suy luận hoặc "Unexplained" (tính lúc đọc, không lưu); cộng item cho trade thật, gộp theo lệnh | qua `GET /api/cash-flows` |
| `trades.py` (N4) | Sau mỗi `record`: fill Spot (`myTrades` theo cặp) và Convert vào bảng `trades`; lần đầu định giá lại dòng tiền cũ theo giá đóng cửa ngày | collector |
| `cost_basis.py` (N4) | Giá vào bình quân theo coin trên toàn danh mục, PnL chưa thực hiện trên phần sổ giải thích được (`tracked`), cờ `estimated`, bảng điểm các lần mua | `GET /api/analytics/cost-basis` |
| `backup.py` | Sao lưu DB: bản sao nhất quán (backup API của SQLite, kiểm `integrity_check`), gzip vào `backups/` cạnh DB, giữ `backup.keep` bản; đẩy lên cloud bằng `rclone copy` khi có `backup.remote` (app không giữ thông tin đăng nhập cloud) | chạy sau `record` mỗi `backup.every_hours`; `GET`/`POST /api/backup`, `GET /api/backup/download` |
| `activity.py` | Dòng thời gian đọc được và sửa được của `cash_flows` (reward Earn gộp theo ngày trong SQL), cộng trade thật, item suy luận của `reconcile.py` (chỉ 90 ngày gần nhất) và ghi chú của từng ngày | `GET`/`PATCH /api/cash-flows` |

Mọi engine là hàm thuần trên dữ liệu thường; chỉ các loader chạm SQLite. Chỉ dùng numpy và stdlib.
Công thức và thuật toán: [`SPEC.md`](SPEC.md).

### 3.5 Dashboard — `web_api.py`, `static/`

**API** (mọi `/api/*` trừ `/api/auth` cần token):

| Nhóm | Route |
|---|---|
| Danh mục | `GET /api/portfolio` · `POST /api/portfolio/refresh` · `GET /api/portfolio/history` |
| Phân tích | `GET /api/portfolio/attribution` · `GET /api/portfolio/pnl` · `GET /api/analytics/quant` · `GET /api/analytics/allocation` · `GET /api/analytics/earn` · `GET /api/analytics/cost-basis` |
| Dòng tiền | `GET /api/cash-flows` · `GET /api/cash-flows/{id}/candidates` · `PATCH /api/cash-flows/{id}` |
| Nhật ký | `PUT /api/notes/{day}` (body rỗng = xoá) |
| Sao lưu | `GET /api/backup` (trạng thái) · `POST /api/backup` (sao lưu ngay) · `GET /api/backup/download` (tải bản mới nhất) |
| Cấu hình | `GET`/`POST /api/settings` · `POST /api/settings/allocation` · `POST /api/settings/test-binance` · `POST /api/settings/test-wallet` |
| Khác | `POST /api/auth` · `GET /api/health` · `/api/docs` |

**Xác thực:** token từ `PORTFOLIO_WEB_TOKEN` hoặc `settings.web_token`. Gửi bằng
`Authorization: Bearer`, hoặc đổi lấy cookie phiên đã ký ở màn hình unlock (HMAC theo token, 90
ngày, không chứa token). Credential sai bị giới hạn 20 lần mỗi 5 phút mỗi client. `serve` từ chối
bind ngoài loopback khi chưa đặt token.

**PWA** (Vue 3, Tailwind, Lightweight Charts): ba tab ở thanh dưới.
- **Portfolio:** Net Worth, chip 24h, chart có scrub, card "Why it moved", danh sách tài sản theo
  All / Binance / Personal (Coins | Wallets). Dòng trạng thái dưới hero báo sức khoẻ dữ liệu (tuổi
  snapshot, "stale" khi quá 130 phút, số hố 24h) và pill lệch peg (N2.4, N3.3).
- **Analytics:** các thẻ gập được (A1), hai nhóm "Over the period" (Why it moved, Performance, Risk)
  và "Right now" (Earn income, Allocation).
- **Activity:** dòng thời gian nạp/rút/chuyển/lãi, trade suy luận (tag "inferred") và dòng
  "Unexplained", bộ lọc, hàng chờ xác nhận.
- Chế độ riêng tư làm mờ mọi số tiền (`.priv`). Settings sửa Binance key, ví, tài sản manual; secret
  chỉ hiển thị dạng đã che.
- Service worker chỉ đăng ký trên HTTPS hoặc `localhost`; truy cập qua HTTP trên LAN/Tailscale nên
  hiện không có offline shell.

### 3.6 Gateway public — `api_gateway.py`

Một route: `GET /v1/crypto/portfolio/total?currency=USD` với header `X-API-Key`. Trả tổng, tổng theo
nguồn, thời điểm và cờ `stale`. Mọi request không hợp lệ (sai key, path, method, Host) nhận cùng một
`404`. Rate limit 30 request/phút mỗi client. Chỉ bind loopback, đi ra Internet qua Cloudflare Tunnel.

### 3.7 CLI — `main.py`

`show` · `record` · `chart` · `history` · `backfill` · `pnl` · `export` · `backup` · `serve`. Trong `record`:
kiểm peg stablecoin (`peg_checks`). Sau mỗi `record`: ghi dòng tiền của tài sản manual, đồng bộ lịch
sử Binance, đồng bộ lệnh Spot và Convert, rồi sao lưu nếu tới hạn. Lỗi ở các bước này không làm
hỏng snapshot. `export`
ghi một CSV cho mỗi view `v_*` và `notes.csv`.

## 4. Bảo mật

| Bí mật | Nơi lưu | Ai đọc | Nếu lộ |
|---|---|---|---|
| **T1** Binance key + secret | `config.yaml` (0600) hoặc `.env` | collector, dashboard | Đọc được số dư Binance. Không trade, không rút, với điều kiện key chỉ có quyền đọc |
| **T2** Token dashboard | `.env` hoặc `config.yaml` | dashboard | Đọc số dư và **ghi `config.yaml`** (đổi key, thêm/xoá ví) |
| **T3** Key gateway | `.env.gateway` (0600) | gateway | Chỉ đọc được tổng và tổng theo nguồn |
| **T4** Dữ liệu nhạy cảm | `config.yaml`, `portfolio_history.db` (0600) | cả ba | Lộ quy mô tài sản và địa chỉ ví |

- **Bất biến:** gateway (T3) không thể leo lên T1/T2.
- **Mạng:** dashboard chỉ truy cập qua LAN và Tailscale, không mở ra Internet. Phương án mở public
  bằng Cloudflare Access được để dành ở [`AUTH_SPEC.md`](AUTH_SPEC.md).
- **Lưới an toàn cuối:** phía Binance, key chỉ bật *Enable Reading* và có IP whitelist.
- **Còn mở:** dashboard chạy HTTP thuần trong mạng riêng (chấp nhận); `/api/docs` tồn tại sau token;
  RPC công khai thấy địa chỉ ví và IP của máy.

## 5. Vận hành

| Hạng mục | Cấu hình |
|---|---|
| Collector | crontab người dùng, `main.py record` mỗi giờ |
| Dashboard | `systemctl --user … crypto-web`, cổng 8088, token trong `.env` |
| Gateway | `systemctl --user … crypto-api-gateway`, cổng 8090, sau `cloudflared` |
| Backup | Chưa có lịch. Migration tự sao lưu file trước khi chạy |
| Test | `python -m pytest -q` (offline, mọi HTTP được mock) |

## 6. Bản đồ thư mục

| Đường dẫn | Vai trò |
|---|---|
| `main.py` | CLI |
| `config.py`, `models.py` | Cấu hình; `Holding`, `AssetRow`, `Portfolio` |
| `clients/` | Binance, on-chain |
| `services/` | `portfolio`, `db`, `manual`, `cash_flows`, `activity`, `reconcile`, `peg`, `backfill`, `resample`, `visualizer` |
| `services/analytics/` | `attribution`, `pnl`, `quant`, `allocation`, `earn` |
| `web_api.py`, `static/` | Dashboard: API + PWA |
| `api_gateway.py` | Gateway public chỉ đọc |
| `deploy/` | Hai unit systemd |
| `tests/` | Toàn bộ test; `conftest.py` cô lập khỏi `.env` thật |
| `scripts/archive/` | Script chạy một lần đã xong, giữ để tham khảo |
| `docs/` | Tài liệu này, `SPEC.md`, `LEDGER.md`, `AUTH_SPEC.md` |
