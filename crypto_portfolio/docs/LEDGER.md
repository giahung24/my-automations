# LEDGER — sổ theo dõi `crypto_portfolio`

> Trạng thái từng task, quyết định và changelog. Việc sắp làm ở [`../ROADMAP.md`](../ROADMAP.md),
> đặc tả và AC ở [`SPEC.md`](SPEC.md).
>
> **Đối soát với code ngày 2026-11-27** (thay cho D17): mỗi dòng dưới đây được kiểm bằng cách tìm
> module, endpoint và test tương ứng trong repo. Đây là kiểm tra "có code và có test", **không** phải
> chạy lại từng Acceptance Criteria; dòng nào AC chưa đạt đủ được ghi là `PARTIAL` kèm phần thiếu.

## 1. Quy ước

| Trạng thái | Ý nghĩa |
|---|---|
| `DONE` | Có code và test trong repo. |
| `PARTIAL` | Có code, nhưng thiếu một phần AC (ghi ở cột Ghi chú). |
| `TODO` | Chưa có code. |
| `DEFERRED` | Tạm hoãn theo quyết định (D16); spec giữ nguyên. |

- **ID cố định**, không tái sử dụng: `P0.n`, `F1.n`…`F3.xn` (Phase 1), `M4.n`…`M8.n` (Phase 2),
  `N1`…`N9` (Phase 3, tách `N<k>.<n>` khi mở).
- Ưu tiên: **P0** chặn mọi thứ · **P1** cốt lõi · **P2** quan trọng · **P3** nice-to-have.
- Khi đóng một task: sửa dòng ở đây, ghi một dòng vào §6, và nếu code lệch đặc tả thì sửa bảng
  "Chỗ code đã đi khác đặc tả" ở đầu SPEC trong cùng commit.
- **Definition of Done:** `pytest -q` xanh và test mới chạy offline · mỗi AC có test hoặc bước kiểm
  tay được ghi lại · `GET` mới không gọi mạng · endpoint mới nằm sau token, gateway không thêm route
  (ngoại lệ duy nhất: D11) · số tiền trên UI mang `.priv` · chỉ số định lượng kèm `n_obs` và
  `sufficient` · cập nhật ARCHITECTURE khi schema, endpoint hay luồng dữ liệu đổi.

## 2. Phase 1 — P0, F1, F2, F3

Commit chính: P0.1 `7d29fd5` · P0.2+P0.3 `fed6a63` · P0.4 `3d80f35` · P0.8 `56ebc4f` · F1 `6f764e1` ·
F2 `ee13944` · F3 `65caece` (đều 2026-11-25) · P0.6 `b6ee685` (2026-11-26).

| ID | Task | Ưu tiên | Trạng thái | Bằng chứng / Ghi chú |
|---|---|---|---|---|
| P0.1 | Migration `PRAGMA user_version` + backup | P0 | DONE | `services/db.py` (`MIGRATIONS`, hiện v5), `tests/test_all.py` |
| P0.2 | `snapshot_sources`, `snapshots.trigger` | P0 | DONE | `tests/test_snapshot_sources.py` |
| P0.3 | `detail`, `price_id` trong `snapshot_assets` | P0 | DONE | `tests/test_snapshot_sources.py` |
| P0.4 | WAL | P0 | DONE | `tests/test_wal.py` |
| P0.5 | Cron `record` 1h/lần | P0 | DONE | Cấu hình trên máy chủ (crontab), không nằm trong repo |
| P0.6 | Token + unit systemd cho dashboard | P0 | DONE | `main.py serve` từ chối bind thiếu token, `deploy/crypto-web.service`. Bind `0.0.0.0` kèm token thay vì một IP LAN cụ thể (xem AUTH_SPEC) |
| P0.7 | Multicall3 cho ERC-20 | P0 | TODO | Vẫn một `eth_call` mỗi token |
| P0.8 | Backfill 5 năm (checkpoint tháng) | P0 | DONE | `services/backfill.py`, `tests/test_backfill.py` |
| F1.1–F1.5 | Loader, engine phân rã, `chained`/`endpoint`, bucket `revaluation`/`data_gap`, endpoint | P1 | DONE | `services/analytics/attribution.py`, `GET /api/portfolio/attribution`, `tests/test_analytics.py` |
| F1.6 | Câu `summary` | P2 | TODO | |
| F1.7 | UI "Vì sao biến động?" | P1 | PARTIAL | Có card (24H/7D/30D, bucket, diverging bar, banner). Thiếu bottom sheet và waterfall |
| F1.8 | Tách quantity thành flow/transfer/income/trade | P2 | TODO | Nên chờ N4 |
| F1.9 | CLI `main.py why` | P3 | TODO | |
| F2.1 | Migration v4 `cash_flows`, `sync_state` | P1 | DONE | `services/db.py` |
| F2.2 | Lịch sử nạp/rút Binance | P1 | DONE | `clients/binance.py` (`HistoryStream`); thêm fiat và P2P |
| F2.3 | `sync()` + cursor + nối vào `record` | P1 | DONE | `services/cash_flows.py` (`sync_after_record`), `tests/test_cash_flows.py` |
| F2.4 | Chuẩn hoá network/địa chỉ/thời gian | P1 | DONE | `chain_for_network`, `normalize_address`, `to_iso_utc` |
| F2.5 | Phát hiện flow on-chain từ delta snapshot | P1 | TODO | Chỉ Binance và tài sản manual sinh flow |
| F2.6 | Matcher (address, heuristic, một-một) | P1 | PARTIAL | `match_transfers` có đủ ba tầng và gán một-một. AC cần vế on-chain (F2.5) nên chưa đạt; dòng `fee` riêng chưa được kiểm |
| F2.7 | In-transit + chốt | P1 | PARTIAL | `in_transit_hours` dùng làm cửa sổ ghép cặp và cờ `needs_review`; trạng thái `pending` theo đặc tả chưa được kiểm |
| F2.8 | Income: lãi Earn, dividend | P2 | PARTIAL | Lãi Earn flexible + locked có. Chưa có `assetDividend` (airdrop) |
| F2.9 | Xác minh `tx_hash` on-chain + tầng swap | P2 | TODO | Chỉ so khớp hai hash giống nhau |
| F2.10 | `GET`/`PATCH /api/cash-flows` | P2 | DONE | Qua M4.1, M4.2 |
| F2.11 | UI dòng tiền | P2 | DONE | Qua M4.4, M4.6 (tab Activity) |
| F2.12 | `change_24h.flow_adjusted_usd` | P2 | TODO | `GET /api/portfolio/pnl` cho PnL đã khử dòng tiền 7d/30d, nhưng chip 24h vẫn là ΔNAV thô |
| F2.13 | Settings Test: quyền đọc lịch sử | P3 | TODO | |
| F2.14 | Lịch sử tx qua explorer API | P3 | TODO | |
| F3.S1 | Chuỗi ngày + Modified Dietz | P1 | DONE | `services/analytics/quant.py`, `tests/test_quant.py` |
| F3.T1 | TWR | P1 | DONE | `quant.py` |
| F3.T2 | MWR / XIRR | P2 | TODO | |
| F3.P1 | PnL theo cửa sổ + endpoint | P1 | PARTIAL | `services/analytics/pnl.py`, `GET /api/portfolio/pnl`, CLI `pnl`. Chỉ 7d/30d mặc định; chưa có 90d/YTD/ALL |
| F3.P2 | UI thẻ PnL | P2 | TODO | Endpoint chưa được UI gọi |
| F3.B1 | Bảng `benchmark_prices` | P2 | TODO | Bảng cũ ghi DONE là sai: không có bảng này, giá BTC/ETH lấy từ snapshot |
| F3.B2 | SPX | P2 | TODO | |
| F3.B3 | Base 100 | P2 | DONE | BTC/ETH, trong `quant.py` |
| F3.B4 | β, ρ, TE, α, IR | P2 | TODO | |
| F3.B5 | UI chart so sánh benchmark | P2 | DONE | Thẻ Insights (tab Analytics), không có SPX |
| F3.R1 | MDD + underwater | P2 | DONE | `quant.py` |
| F3.R2 | Volatility năm hoá | P2 | DONE | `quant.py` |
| F3.R3–R6 | Sharpe, Sortino, VaR/CVaR, VaR theo tỷ trọng | P2/P3 | TODO | |
| F3.R7 | Endpoint risk + UI underwater | P2 | PARTIAL | Underwater nằm trong `GET /api/analytics/quant` và thẻ Insights; không có endpoint risk riêng |
| F3.A1–A4 | Allocation: config, drift, ràng buộc, endpoint + UI | P2/P3 | DONE | Qua M7.1–M7.4 |
| F3.C1–C3 | Concentration (HHI, CEX vs self-custody) | P1 | TODO | |
| F3.U1 | Màn Insights | P2 | DONE | Tab Analytics |
| F3.U2 | CLI `main.py report` | P3 | TODO | |

## 3. Phase 2 — M4…M8

Commit chính: M6 `ce47a46` · M4 `13dc192` · M7 `80cde3d` · UI `5fd0580` (2026-11-25) · góp vốn một
phần `53b4309` (2026-11-26).

| ID | Task | Ưu tiên | Trạng thái | Bằng chứng / Ghi chú |
|---|---|---|---|---|
| M4.1 | `GET /api/cash-flows` (lọc, cursor, gộp cặp, facets) | P1 | DONE | `services/activity.py`, `tests/test_activity.py` |
| M4.2 | `PATCH /api/cash-flows/{id}` + `candidates` | P1 | DONE | `confirm_flow`, `reset_flow`, `transfer_candidates` |
| M4.3 | Bottom nav 3 tab | P1 | DONE | Portfolio · Analytics · Activity |
| M4.4 | Timeline tab Activity | P1 | DONE | `static/app.js`, `static/index.html` |
| M4.5 | Chip lọc nguồn / loại | P2 | DONE | Dùng `facets`. Dữ liệu on-chain trống cho tới F2.5 |
| M4.6 | Hàng chờ + xác nhận thủ công | P2 | DONE | Banner "N transactions need a confirmation" |
| M4.7 | Badge `needs_review` trên tab | P3 | TODO | Chỉ có banner trong tab |
| M5.1–M5.5 | Telegram: client, notifier, digest, cảnh báo, heartbeat | P1 | DEFERRED | D16, wave W7 |
| M5.6 | Cảnh báo hạn API key | P2 | TODO | Bị chặn bởi W7 |
| M5.7 | Dead-man's switch ngoài máy | P3 | TODO | Bị chặn bởi W7 |
| M6.1 | Config `manual_assets` + validate | P1 | DONE | `config.py`, `tests/test_manual_link.py` |
| M6.2 | Nguồn `manual` + `manual_usd` (migration v5) | P1 | DONE | `services/manual.py`, `services/db.py` |
| M6.3 | Đổi số lượng manual thành dòng tiền + ghép với lệnh rút Binance | P1 | DONE | `record_manual_flows`, `match_manual_transfers` |
| M6.4 | UI + form Settings | P2 | DONE | Tab Personal (Coins/Wallets), `linked_wallet`, nhắc 90 ngày cho staking |
| M6.5 | Watch-only BTC (địa chỉ / xpub) | P3 | TODO | |
| M7.1 | Config `allocation` + validate | P2 | DONE | `config.py` (`parse_allocation`) |
| M7.2 | Engine drift, `target` / `band_edge` | P2 | DONE | `services/analytics/allocation.py`, `tests/test_allocation.py` |
| M7.3 | Ràng buộc thanh khoản | P2 | DONE | `apply_liquidity` (locked, `liquid: false`) |
| M7.4 | Endpoint + thẻ Allocation | P2 | DONE | `GET /api/analytics/allocation` |
| M7.5 | Chế độ `contribution` | P2 | DONE | Thêm góp vốn một phần `greedy`/`proportional`, ngoài đặc tả |
| M7.6 | Cảnh báo drift qua Telegram | P3 | TODO | Bị chặn bởi W7 |
| M7.7 | Sửa mục tiêu trong Settings + lịch sử drift | P3 | PARTIAL | Chỉ có công tắc bật/tắt (`POST /api/settings/allocation`); mục tiêu vẫn sửa trong `config.yaml` |
| M8.1–M8.3 | Gateway `/summary`, mô hình bảo mật, widget iOS | P2 | DEFERRED | D16, wave W8 |
| M8.4, M8.5 | Sparkline, Shortcut / Android | P3 | TODO | Bị chặn bởi W8 |

**Phase 3 (N1–N9):** N4–N9 `TODO`. Bảng xếp hạng và thẻ ý tưởng nằm ở ROADMAP §2.

| Task | Việc | Trạng thái | Ghi chú |
|---|---|---|---|
| A1 | Analytics gập được, hai nhóm | DONE | `static/` |
| N1.1–N1.4 | Earn: APR niêm yết, APR thực nhận 7d/30d, thu nhập luỹ kế, thẻ "Earn income" | DONE | `services/analytics/earn.py`, `GET /api/analytics/earn`; AC "USDC ≈ 5.0%" chưa đo trên dữ liệu thật |
| N2.1–N2.4 | Phần dư, trade suy luận, dòng "Unexplained", sức khoẻ dữ liệu trên dòng trạng thái | DONE | `services/reconcile.py`; chỉ Binance; AC 25–27/11 chưa chạy trên dữ liệu thật |
| N3.1–N3.3 | Peg check: bảng `peg_checks`, ghi mỗi snapshot, pill cảnh báo | DONE | `services/peg.py`; schema v6 |

## 4. Bản thẩm định 2026-11-25 và tình trạng hôm nay

SPEC nhắc tới các mã này. Bản thẩm định gốc nằm trong lịch sử git của `SYSTEM_ARCHITECTURE.md`.

| Mã | Phát hiện lúc đó | Hôm nay |
|---|---|---|
| A1 | Không có Multicall, một `eth_call` mỗi token | Còn (P0.7) |
| A4 | Chip 24h là ΔNAV thô, tính cả nạp/rút | Còn (F2.12); đã có attribution và PnL khử dòng tiền |
| A5 | Nguồn lỗi bị ghi thành 0 | Đã sửa (P0.2) |
| A6 | `snapshot_assets` mất chain/ví | Đã sửa (P0.3) |
| A8 | Không có migration | Đã sửa (P0.1) |
| A9 | Gateway có thể gặp `database is locked` | Đã sửa (P0.4) |
| SEC-1 | Dashboard mở trên LAN không token | Đã sửa (P0.6) |
| SEC-2 | Dashboard chạy ad-hoc, không có unit | Đã sửa (`deploy/crypto-web.service`) |
| SEC-3 | Gateway chưa được commit | Đã sửa |
| SEC-4 | DB mode 0644 | Đã sửa: tạo 0600 và tự siết file cũ |
| SEC-5 | Dashboard chạy HTTP thuần | **Chấp nhận**: LAN và tailnet là mạng riêng (AUTH_SPEC). Hệ quả: service worker không đăng ký được |
| SEC-6 | `/api/docs` tồn tại sau token | Còn |
| SEC-7 | RPC public thấy địa chỉ ví và IP | Còn; dùng RPC riêng qua `chains.<name>.rpcs` nếu cần |

## 5. Nhật ký quyết định

| ID | Quyết định | Lý do |
|---|---|---|
| D1 | Attribution tính ở grain `(symbol, source)`, hiển thị theo `symbol` | Giá trung bình đổi khi chuyển giữa nguồn sẽ sinh price effect giả |
| D2 | Analytics chỉ dùng numpy + stdlib | Dependency nhẹ, dữ liệu nhỏ, tự viết và test được |
| D3 | Benchmark BTC/ETH lấy từ Binance, không CoinGecko | Không cần key, cùng nguồn giá với danh mục |
| D4 | Ranh giới ngày mặc định UTC | Khớp nến ngày crypto |
| D5 | MDD và benchmark dùng chỉ số TWR, không dùng NAV | Rút tiền không phải drawdown |
| D6 | Return ngày bằng Modified Dietz với timestamp flow thật | Binance cho timestamp chính xác |
| D7 | Ghép chuyển khoản nhiều tầng, tầng xác định trước heuristic | Rút tới địa chỉ ví đã cấu hình là bằng chứng chắc chắn |
| D8 | Dữ liệu ngoài được collector đồng bộ vào SQLite; `GET` không gọi mạng | Mở app nhanh, không lộ rate limit ra UI |
| D9 | Gateway public không mở thêm route | Lộ key gateway chỉ lộ đúng một con số |
| D10 | Không annualize return của kỳ dưới 1 năm | Con số năm hoá từ 2 tuần gây hiểu nhầm |
| D11 | Gateway mở đúng một route `/summary`, tắt mặc định (sửa D9) | Cho widget iOS; chưa có hiệu lực vì W8 đang hoãn |
| D12 | Telegram chỉ gửi đi, không nhận lệnh | Không thêm bề mặt tấn công |
| D13 | Tài sản manual khai trong `config.yaml`, lịch sử ở `snapshot_assets` | Cùng chỗ với `wallets`, không thêm schema |
| D14 | Ví lạnh có địa chỉ EVM/Solana thì theo dõi watch-only, không dùng manual | Số dư tự cập nhật |
| D15 | Watchdog chạy bằng cron riêng, tách khỏi `record` | Job chết không thể tự báo mình chết |
| D16 | **Hoãn** W7 (M5.1–M5.5) và W8 (M8.1–M8.3) từ 2026-11-26 | Jean: chưa cần. Spec và AC giữ nguyên |
| D17 | ~~Không lật trạng thái khi sắp xếp roadmap~~ | Hết hiệu lực: thay bằng lượt đối soát 2026-11-27 (D18) |
| D18 | Tách tài liệu: `ROADMAP.md` (việc sắp làm), `docs/LEDGER.md` (trạng thái), `docs/SPEC.md` (đặc tả, giữ số mục). Trạng thái ghi theo code, ba mức DONE/PARTIAL/TODO | File cũ 1.790 dòng trộn ba vai trò và bảng trạng thái trễ so với code |
| D19 | Dashboard chỉ truy cập qua LAN và Tailscale, không mở public; phương án Cloudflare Access để dành | Xem AUTH_SPEC |

## 6. Changelog

| Ngày | Thay đổi |
|---|---|
| 2026-11-25 | Lập đặc tả Phase 1 (P0, F1, F2, F3: 56 task). Trong ngày: xong P0.1–P0.4, P0.8, F1.1–F1.5, lõi F2 (Binance), lõi F3 (gộp vào `quant.py`). |
| 2026-11-25 | Thêm Phase 2 (M4–M8: 31 task), D11–D15. Trong ngày: xong M6, M4, M7 và UI ba tab. |
| 2026-11-26 | Hoãn Telegram và widget (D16). Đánh số lại wave: W1–W3 Phase 2, W4–W6 Phase 3, W7–W8 hoãn. |
| 2026-11-26 | Bảo mật dashboard (P0.6): token bắt buộc ngoài loopback, cookie phiên đã ký, rate limit, DB 0600, unit systemd. Thêm góp vốn một phần cho Allocation. |
| 2026-11-27 | Thêm Phase 3 (N1–N9), gộp từ 7 đề xuất của Fable 5.2 và 6 gợi ý của Jean/assistant. |
| 2026-11-27 | Mở W4: tách N1.1–N1.4, N2.1–N2.4, N3.1–N3.3 trong ROADMAP §2. Chốt: N1 không dựng lại số dư Earn quá khứ (chỉ tổng thu nhập); N2 tính trade suy luận lúc đọc, gộp Binance theo symbol; thứ tự N3 → N1 → N2. |
| 2026-11-27 | Thêm `DESIGN.md` (rút từ `static/`) và sơ đồ vị trí UI cho N1–N9. Thêm A1 (Analytics gập được, hai nhóm). Sửa N1.4 (thẻ Earn sang Analytics) và N2.4 (dùng dòng trạng thái sẵn có, không thêm badge). UI giữ tiếng Anh. |
| 2026-11-27 | Xong code W4: A1, N1, N2, N3. Schema v6 (`snapshot_assets.listed_apr`, `peg_checks`). `earn_backfill_days` mặc định 365 → 1827. N2 thu hẹp về Binance (on-chain chờ F2.5). Test 595 → 613. |
| 2026-11-27 | Tách `ROADMAP_LEDGER.md` thành ROADMAP / LEDGER / SPEC (D18); đối soát toàn bộ trạng thái với code. Sửa các dòng sai của bảng cũ: F3.B1 không phải DONE; F2.2–F2.4, F2.10, F2.11, M4.1–M4.6, M6.1–M6.4, M7.1–M7.5 thực tế đã xong. Dời test vào `tests/`, script một lần vào `scripts/archive/`. |
