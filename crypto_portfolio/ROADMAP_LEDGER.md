# ROADMAP_LEDGER — `crypto_portfolio`

> **Vai trò của tài liệu:** vừa là **đặc tả kỹ thuật** (technical spec) vừa là **sổ theo dõi tiến độ**
> (ledger) cho ba nhóm tính năng Jean đã ưu tiên:
>
> | Epic | Tên | Câu hỏi nghiệp vụ |
> |---|---|---|
> | **F1** | Attribution Analysis | "Vì sao hôm nay tài sản biến động?" |
> | **F2** | Cash Flow Engine & Transfer Matching | "Biến động đó là lãi/lỗ thật hay chỉ là tiền nạp/rút/chuyển ví?" |
> | **F3** | Quantitative Portfolio Intelligence | "Danh mục của tôi hoạt động tốt đến đâu, rủi ro đến đâu, so với thị trường ra sao?" |
>
> **Đầu vào:** hiện trạng ghi trong [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md) ngày 2026-11-25
> (16 snapshot, từ 2026-11-22 đến 2026-11-25, NAV ≈ $50.8k, ≈ 93% nằm ở Binance Simple Earn).
> Các mã `A1…A9`, `SEC-1…SEC-7` trỏ về §4.4 và §6 của tài liệu đó.
>
> **Ngày lập:** 2026-11-25 · **Branch:** `claude/script-preparation-docs-944f7e`

---

## Mục lục

0. [Hướng dẫn sử dụng Ledger](#0-hướng-dẫn-sử-dụng-ledger)
1. [P0: Nền móng bắt buộc (prerequisites)](#1-p0-nền-móng-bắt-buộc)
2. [Epic F1: Attribution Analysis](#2-epic-f1-attribution-analysis-vì-sao-hôm-nay-biến-động)
3. [Epic F2: Cash Flow Engine & Transfer Matching](#3-epic-f2-cash-flow-engine--internal-transfer-matching)
4. [Epic F3: Quantitative Portfolio Intelligence](#4-epic-f3-quantitative-portfolio-intelligence)
5. [Master Ledger Summary](#5-master-ledger-summary)
6. [Lộ trình milestone & đồ thị phụ thuộc](#6-lộ-trình-milestone--đồ-thị-phụ-thuộc)
7. [Nhật ký quyết định (decision log)](#7-nhật-ký-quyết-định)
8. [Changelog của Ledger](#8-changelog-của-ledger)

---

## 0. Hướng dẫn sử dụng Ledger

### 0.1 Quy ước trạng thái

| Ký hiệu | Trạng thái | Ý nghĩa |
|---|---|---|
| `[ ]` | **TODO** | Chưa bắt đầu. |
| `[~]` | **IN PROGRESS** | Đang làm, đã có branch hoặc commit dở dang. Ghi tên branch. |
| `[x]` | **DONE** | Đạt **toàn bộ** Acceptance Criteria (AC) và Definition of Done (§0.3). |
| `[-]` | **DROPPED** | Bỏ, kèm một dòng lý do. Không xoá dòng khỏi Ledger. |

Độ ưu tiên: **P0** = chặn mọi thứ phía sau · **P1** = giá trị cốt lõi, làm ngay sau P0 ·
**P2** = quan trọng, cần thêm dữ liệu hoặc phụ thuộc P1 · **P3** = nice-to-have.

### 0.2 Quy tắc cập nhật

1. Mỗi task có một **ID cố định** (`P0.3`, `F2.4`, `F3.R2`…). ID không bao giờ được tái sử dụng.
2. Khi đổi trạng thái, cập nhật **cả hai chỗ**: checklist trong Epic **và** bảng Master (§5).
3. Khi chuyển sang `[x]`, điền đuôi dòng theo mẫu:
   ```
   - [x] **F1.2** Engine phân rã … — ✅ 2026-12-03 · `a1b2c3d` · services/analytics/attribution.py, test_analytics.py::test_attribution_identity
   ```
   Bắt buộc có: **ngày (YYYY-MM-DD)**, **short SHA commit**, **file chính**, **tên test chứng minh AC**.
4. Khi chuyển sang `[~]`, ghi `🔧 <branch> · <ngày bắt đầu>`.
5. Mọi thay đổi phạm vi (thêm/bỏ task, đổi AC) phải được ghi một dòng vào §8 Changelog.
6. Nếu triển khai lệch khỏi spec (đổi công thức, đổi schema), sửa spec **trong cùng commit** với code.
   Ledger phải luôn mô tả đúng cái đang chạy.

### 0.3 Definition of Done (áp dụng cho mọi task)

- [ ] `pytest -q` xanh. Test mới chạy offline (HTTP được mock, như các test hiện có).
- [ ] Mọi AC của task đều có ít nhất một test hoặc một bước kiểm chứng thủ công được ghi lại.
- [ ] Endpoint `GET` mới **không gọi mạng** (cùng nguyên tắc với `GET /api/portfolio`). Dữ liệu ngoài
      (lịch sử nạp/rút, giá benchmark) được collector đồng bộ vào SQLite trước.
- [ ] Endpoint mới nằm sau middleware token (`require_token`). `api_gateway.py` **không** được mở thêm
      route nào (bất biến T3, §4.1 của tài liệu kiến trúc).
- [ ] Mọi con số tiền tệ mới trên UI mang class `.priv` (privacy blur).
- [ ] Mọi chỉ số định lượng trả về kèm `n_obs` và cờ `sufficient` khi chưa đủ mẫu. Không bao giờ hiển
      thị Sharpe/Beta/VaR tính trên 4 điểm dữ liệu như thể nó có ý nghĩa.
- [ ] `SYSTEM_ARCHITECTURE.md` được cập nhật nếu schema, endpoint hoặc luồng dữ liệu thay đổi.

---

## 1. P0: Nền móng bắt buộc

Ba Epic đều đọc lịch sử snapshot. Lịch sử hiện tại có bốn khuyết tật làm sai mọi phép tính phía sau
(A5, A6, A8, A9), và chỉ có **3 ngày** dữ liệu với nhịp ghi 12h/lần. Các task P0 phải xong trước, hoặc
ít nhất là trước task F tương ứng ghi trong cột "Chặn".

| ID | Task | Lý do (tham chiếu) | Chặn | File đích |
|---|---|---|---|---|
| P0.1 | Cơ chế migration bằng `PRAGMA user_version` | A8: không thêm được bảng/cột một cách an toàn | mọi thứ | `services/db.py` |
| P0.2 | Lưu trạng thái từng nguồn mỗi snapshot | A5: nguồn lỗi được ghi thành `0`, tạo "hố" giả | F1, F3 | `services/db.py`, `services/portfolio.py` |
| P0.3 | Lưu `detail` + `price_id` trong `snapshot_assets` | A6: mất thông tin chain/ví, không phát hiện được flow on-chain theo ví | F2 | `services/db.py` |
| P0.4 | Bật WAL | A9: gateway có thể nhận 503 khi collector đang ghi | — | `services/db.py` |
| P0.5 | Tăng nhịp cron lên 1h/lần | Độ phân giải analytics + 96% thời gian gateway báo `stale` | F3 | crontab, `SYSTEM_ARCHITECTURE.md` §5 |
| P0.6 | Bật `web_token` + bind LAN cụ thể | SEC-1: endpoint mới sẽ lộ thêm tx hash và địa chỉ ví | F1–F3 UI | env, unit systemd |
| P0.7 | Multicall3 `aggregate3` cho ERC-20 | A1: nhịp 1h × ~70 RPC/lần sẽ chạm rate limit RPC public | P0.5 | `clients/onchain.py` |

### 1.1 Đặc tả P0.1 — migration

```python
MIGRATIONS: list[str] = [
    # v1: schema gốc (đã có). Áp dụng no-op cho DB cũ có user_version = 0.
    "",
    # v2 (P0.2 + P0.3)
    """
    ALTER TABLE snapshot_assets ADD COLUMN detail   TEXT NOT NULL DEFAULT '';
    ALTER TABLE snapshot_assets ADD COLUMN price_id TEXT NOT NULL DEFAULT '';
    CREATE TABLE IF NOT EXISTS snapshot_sources (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        source      TEXT    NOT NULL,          -- binance_spot | binance_earn | binance_funding | onchain:<chain>/<label>
        status      TEXT    NOT NULL,          -- ok | error | disabled
        error       TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (snapshot_id, source)
    );
    ALTER TABLE snapshots ADD COLUMN trigger TEXT NOT NULL DEFAULT 'unknown';  -- cron | refresh | unknown
    """,
    # v3 (P0.8): snapshots.is_backfilled — checkpoint tháng dựng lại bởi services/backfill.py
    # v4 (F2): cash_flows, sync_state — xem §3.3
    # v5 (F3): benchmark_prices — xem §4.2.2
]
```

- `Database._init_schema` đọc `user_version`, chạy tuần tự các migration còn thiếu **trong một
  transaction**, rồi đặt `user_version = len(MIGRATIONS)`.
- Trước khi migrate, sao lưu file thành `portfolio_history.db.bak_v<old>_<YYYYmmdd_HHMMSS>`.
- **AC:** (a) DB cũ (v0, 16 snapshot) được nâng lên bản mới nhất mà không mất dòng nào; (b) chạy lần hai
  là no-op; (c) migration lỗi ở giữa thì rollback, `user_version` không đổi; (d) gateway (`mode=ro`) vẫn
  đọc được DB sau migrate.

### 1.2 Đặc tả P0.2 — trạng thái nguồn

Granularity của `source` trong `snapshot_sources` phải **mịn hơn** 4 cột `*_usd`: `onchain` cần tách
theo `(chain, wallet)` vì lỗi RPC chỉ làm mất một phần on-chain. Sau P0.2, mọi analytics dùng quy tắc:

> Một cặp snapshot liên tiếp `(t-1, t)` là **degraded** cho nguồn `s` nếu `s` có `status = error` ở
> **một trong hai** snapshot. Phần ΔV của nguồn `s` trong cặp đó **không** được tính là price/quantity
> effect mà dồn vào bucket `data_gap` (F1), và return của cặp đó bị loại khỏi ước lượng σ (F3).

Với 16 snapshot cũ (chưa có status), áp dụng heuristic phát hiện hố: tổng một nguồn giảm > 90% rồi hồi
lại > 90% ở snapshot kế tiếp thì đánh dấu `status = 'inferred_error'`.

**AC:** (a) snapshot ghi khi một chain bị 503 có dòng `error` tương ứng; (b) `GET /api/portfolio` trả
`errors` cả khi đọc từ DB (hiện chỉ có khi fetch live); (c) heuristic backfill có test với một chuỗi hố
tổng hợp.

### 1.3 Ghi chú P0.5 — vì sao 1h

Nhịp 1h cho 24 điểm mỗi ngày: đủ cho attribution 24h dạng chuỗi (§2.2.3), đủ để chọn "giá đóng cửa
ngày" sát 00:00 UTC (§4.1.1), và hạ tỷ lệ `stale` của gateway xuống ~50% (30/60 phút). Muốn `stale`
gần 0 thì đồng thời nâng `GATEWAY_STALE_AFTER_SECONDS` lên 4500. Chi phí: ~8.8k snapshot/năm × ~50
dòng = ~440k dòng `snapshot_assets`/năm, vẫn rất nhỏ với SQLite.

**Checklist P0**

- [x] **P0.1** Migration `PRAGMA user_version` + backup trước khi migrate — AC §1.1 — ✅ 2026-11-25 · commit 7d29fd5]
- [ ] **P0.2** Bảng `snapshot_sources`, cột `snapshots.trigger`, backfill heuristic — AC §1.2
- [ ] **P0.3** Cột `detail`, `price_id` trong `snapshot_assets`; `portfolio_from_db` trả lại `locations` — AC: snapshot mới có `detail` khác rỗng cho mọi dòng on-chain
- [x] **P0.4** `PRAGMA journal_mode=WAL` một lần lúc init — AC: test đọc `mode=ro` trong lúc một transaction ghi đang mở không bị `database is locked` — ✅ 2026-11-25 · commit 3d80f35]
- [x] **P0.5** Cron `0 * * * *` — ✅ 2026-11-25 · `crontab` · Đã cập nhật crontab hệ thống sang nhịp 1h/lần (`0 * * * *`)
- [ ] **P0.6** `PORTFOLIO_WEB_TOKEN` + `--host 192.168.1.66` + unit `deploy/crypto-web.service` (SEC-1, SEC-2) — AC: `GET /api/settings` không token trả 401
- [ ] **P0.7** Multicall3 — AC: số RPC call mỗi refresh giảm từ ~70 xuống ≤ 15 (đo bằng counter trong test mock), kết quả số dư không đổi
- [ ] **P0.8** Backfill lịch sử 5 năm (60 checkpoints tháng): Kéo klines tháng từ Binance / DefiLlama cho BTC/ETH/Stablecoins, tạo 60 snapshot ngày đầu tháng (2021-11 đến 2026-10), gắn cờ `is_backfilled = 1` trong SQLite — AC: có 60 checkpoint lịch sử liên tục, biểu đồ hiển thị được view 1Y/5Y/ALL mà không bị rỗng.

---

## 2. Epic F1: Attribution Analysis ("Vì sao hôm nay biến động?")

### 2.1 Mục tiêu

Hiện chip "24h" chỉ trả lời **bao nhiêu** (ΔNAV thô, A4). F1 trả lời **vì sao**: bao nhiêu đến từ giá
thị trường, bao nhiêu từ thay đổi số lượng (nạp/rút, trade, lãi Earn), và tài sản nào kéo tăng/giảm
mạnh nhất. Ví dụ câu trả lời mong muốn:

> **−$452 (−0.88%) trong 23.9h.** Giá BTC kéo −$310, ETH −$120. Lãi Earn +$6. Không có nạp/rút.

### 2.2 Đặc tả toán học

#### 2.2.1 Phân rã một bước (hai snapshot liên tiếp)

Với tài sản $i$ (định nghĩa grain ở §2.2.2), số lượng $Q$, giá $P$, giá trị $V = Q \cdot P$:

$$\Delta V_i = V_{i,t} - V_{i,t-1}$$

$$\text{Price Effect}_i = Q_{i,t-1} \times (P_{i,t} - P_{i,t-1})$$

$$\text{Quantity/Flow Effect}_i = (Q_{i,t} - Q_{i,t-1}) \times P_{i,t-1}$$

$$\text{Interaction}_i = \Delta Q_i \times \Delta P_i$$

**Đồng nhất thức (bắt buộc kiểm thử):**

$$\Delta V_i = Q_{t}P_{t} - Q_{t-1}P_{t-1} = \underbrace{Q_{t-1}\Delta P}_{\text{price}} + \underbrace{\Delta Q\, P_{t-1}}_{\text{quantity}} + \underbrace{\Delta Q\,\Delta P}_{\text{interaction}}$$

và tổng theo mọi tài sản phải bằng đúng $\Delta \text{NAV}$:

$$\sum_i \Delta V_i = \text{NAV}_t - \text{NAV}_{t-1}$$

Interaction không bị chia vào hai effect kia mà được báo cáo riêng (minh bạch hơn và đúng với spec).
Với nhịp 1h, $|\Delta Q \Delta P|$ thường rất nhỏ. Nếu nó vượt 5% của $|\Delta V_i|$ thì UI hiển thị nó
thành một dòng riêng, còn không thì gộp vào "Khác".

#### 2.2.2 Grain tính toán: `(symbol, source)`, hiển thị theo `symbol`

`snapshot_assets` lưu **một dòng mỗi Holding** (ETH on-chain có 3 dòng cho Ethereum/Arbitrum/Base). Quy
tắc:

1. **Tính** ở grain `(symbol, source)` (sau P0.3 có thể mịn tới `(symbol, source, detail)`):
   $Q = \sum q$, $V = \sum v$, $P = V / Q$ nếu $Q > 0$.
2. **Cộng dồn** các effect lên grain `symbol` để hiển thị. Các effect là cộng tính nên phép cộng này
   chính xác.

Vì sao không tính thẳng ở grain `symbol`: giá trung bình theo giá trị (`aggregate()` bước 4) **dịch
chuyển khi tỷ trọng giữa các nguồn đổi**. ETH ở Binance được định giá bằng ticker, ETH on-chain bằng
DefiLlama, và hai giá lệch nhau vài cent. Chuyển 1 ETH từ Binance sang ví sẽ làm $\bar P_{\text{ETH}}$ đổi
nhẹ dù thị trường đứng yên, tức là sinh ra **price effect giả**. Tính ở `(symbol, source)` loại bỏ hiện
tượng này. Đổi lại, một lệnh chuyển Spot → Earn hiện thành hai quantity effect trái dấu ở grain nguồn,
và chúng triệt tiêu nhau khi gộp lên `symbol`, đúng như mong muốn.

#### 2.2.3 Nhiều bước: phương pháp chuỗi (mặc định) và phương pháp hai đầu mút

Cửa sổ 24h với nhịp 1h có khoảng 24 cặp snapshot. Có hai cách:

| Phương pháp | Công thức | Ưu | Nhược |
|---|---|---|---|
| **`chained`** (mặc định) | $\text{Effect}_i = \sum_{k} \text{Effect}_{i,k}$ trên mọi cặp liên tiếp $(k-1, k)$ trong cửa sổ | Price effect bám theo **số lượng thực tế tại từng thời điểm**. Interaction nhỏ dần khi nhịp dày. Tổng vẫn telescoping chính xác về $\Delta\text{NAV}$. | Phụ thuộc đường đi, tốn nhiều phép tính hơn (không đáng kể). |
| `endpoint` | Áp dụng §2.2.1 trực tiếp giữa snapshot đầu và cuối cửa sổ | Đơn giản, dễ giải thích | Mua BTC ở giữa ngày rồi BTC tăng: phần lãi đó bị dồn vào interaction thay vì price. |

#### 2.2.4 Các trường hợp biên và bucket đặc biệt

| Trường hợp | Xử lý | Bucket |
|---|---|---|
| Tài sản mới xuất hiện ($Q_{t-1}=0$) | Price effect = 0, toàn bộ ΔV là quantity (+ interaction = 0 vì lấy $P_{t-1} := P_t$) | `quantity` |
| Tài sản biến mất ($Q_t=0$) | Lấy $P_t := P_{t-1}$, toàn bộ là quantity | `quantity` |
| $P_{t-1}=0$, $P_t>0$ (vừa được định giá, ví dụ DefiLlama vừa có giá token) | **Không** phải biến động thị trường | `revaluation` |
| $P_{t-1}>0$, $P_t=0$ (mất giá do API) | Tương tự, lấy giá trị bị mất | `revaluation` |
| Nguồn lỗi ở một trong hai snapshot (P0.2) | ΔV của nguồn đó trong cặp | `data_gap` |
| Stablecoin ghim $1.00 | Price effect luôn = 0, mọi thay đổi là quantity (bao gồm lãi Earn USDC) | `quantity` |
| Dust | **Vẫn tính** (DB lưu cả dust). Gộp vào "Khác" khi hiển thị | — |

Bất biến mở rộng:

$$\Delta\text{NAV} = \sum_i(\text{price}_i + \text{quantity}_i + \text{interaction}_i) + \text{revaluation} + \text{data\_gap}$$

#### 2.2.5 Tách quantity effect (v2, sau F2)

Khi bảng `cash_flows` (F2) có dữ liệu, quantity effect được tách tiếp:

$$\text{quantity}_i = \underbrace{\text{flow}_i}_{\text{nạp/rút ngoài}} + \underbrace{\text{transfer}_i}_{\text{chuyển nội bộ (≈ −phí)}} + \underbrace{\text{income}_i}_{\text{lãi Earn, airdrop}} + \underbrace{\text{trade}_i}_{\text{phần dư}}$$

- $\text{flow}_i = \sum$ `value_usd` có dấu của các cash flow ngoài (`is_internal_transfer = 0`, `flow_type ∈ {deposit, withdrawal}`) của tài sản $i$ trong cửa sổ, định giá theo $P_{t-1}$ của cặp chứa flow.
- $\text{income}_i$: từ `flow_type = reward` (F2.8, lãi Simple Earn).
- $\text{trade}_i$ là **phần dư**. Trên toàn danh mục, $\sum_i \text{trade}_i \approx -\text{phí giao dịch}$
  vì đổi USDT lấy BTC chỉ chuyển giá trị giữa hai tài sản. Nếu $|\sum_i \text{trade}_i|$ lớn bất thường
  (> $20 và > 0.2% NAV) thì đó là dấu hiệu **có flow chưa phát hiện được**, và UI hiện dòng
  "Biến động số lượng chưa giải thích được: $X".

### 2.3 Thuật toán trích xuất từ `snapshot_assets`

```text
INPUT : window (vd 24h), group_by ∈ {symbol, source, symbol_source}, method ∈ {chained, endpoint}
1. latest  ← Database.latest()
   ref     ← snapshot_before(latest.ts − window) or snapshot_after(latest.ts − window)   # giống change_since
   if ref is None or ref.id == latest.id → trả 200 với "insufficient_history": true
2. ids     ← SELECT id FROM snapshots WHERE timestamp BETWEEN ref.ts AND latest.ts ORDER BY timestamp
   if method == endpoint: ids ← [ref.id, latest.id]
3. rows    ← SELECT snapshot_id, symbol, source, SUM(quantity) AS q, SUM(value_usd) AS v
             FROM snapshot_assets WHERE snapshot_id IN (:ids)
             GROUP BY snapshot_id, symbol, source
   status  ← SELECT snapshot_id, source, status FROM snapshot_sources WHERE snapshot_id IN (:ids)   # sau P0.2
4. state[snapshot_id][(symbol, source)] = (q, p = v/q if q>0 else 0)
5. for each consecutive (a, b) in ids:
       keys ← state[a].keys() ∪ state[b].keys()          # full outer join làm trong Python
       for key in keys:
           if source(key) degraded in (a, b): data_gap += Vb − Va; continue
           (Qa, Pa), (Qb, Pb) ← state[a].get(key, (0, ·)), state[b].get(key, (0, ·))
           áp dụng §2.2.4 để điền giá thiếu, phân loại revaluation
           price[key] += Qa·(Pb−Pa); qty[key] += (Qb−Qa)·Pa; inter[key] += (Qb−Qa)·(Pb−Pa)
6. Cộng dồn key → group_by. Kiểm tra |Σ effects − (NAV_latest − NAV_ref)| < 1e-6 · max(1, NAV)
   (vi phạm thì log lỗi và trả "identity_error" trong payload, không âm thầm che đi)
7. Sắp xếp theo total_effect, cắt top N tăng / top N giảm, phần còn lại gộp vào "Khác"
```

- Không dùng `FULL OUTER JOIN` của SQLite (chỉ có từ 3.39) để không phụ thuộc phiên bản. Bước 5 làm
  trong Python. Khối lượng 30 ngày × 24 × ~60 key ≈ 43k phép tính, không đáng kể.
- Đặt trong `services/analytics/attribution.py` dưới dạng **hàm thuần** nhận `list[SnapshotState]` để
  test không cần DB.

### 2.4 Thiết kế endpoint

```
GET /api/portfolio/attribution?window=24h&group_by=symbol&method=chained&top=5
```

| Tham số | Kiểu | Mặc định | Ràng buộc |
|---|---|---|---|
| `window` | string | `24h` | regex `^\d{1,3}[hd]$`, tối đa `365d` |
| `group_by` | enum | `symbol` | `symbol` · `source` · `symbol_source` |
| `method` | enum | `chained` | `chained` · `endpoint` |
| `top` | int | `5` | 1–20 |

```jsonc
// số liệu minh hoạ
200 {
  "window": "24h",
  "from": "2026-11-24T00:00:05+00:00", "to": "2026-11-25T00:00:04+00:00",
  "hours": 23.99,                         // thời lượng thật, như change_24h
  "snapshots": 25, "method": "chained", "group_by": "symbol",
  "nav_start": 51230.00, "nav_end": 50778.00,
  "delta_usd": -452.00, "delta_pct": -0.8823,
  "buckets": {                            // tổng các bucket == delta_usd (bất biến)
    "price": -457.80, "quantity": 6.10, "interaction": -0.30,
    "revaluation": 0.00, "data_gap": 0.00,
    // chỉ có sau F2 (v2):
    "flow": 0.00, "transfer": 0.00, "income": 6.10, "trade_residual": 0.00
  },
  "contributors": [                       // mọi nhóm, đã sắp theo total
    {"key": "BTC", "price": -310.2, "quantity": 0.0, "interaction": 0.0, "total": -310.2,
     "share_of_move": 0.686, "weight_start": 0.412, "price_change_pct": -1.47}
  ],
  "top_gainers": ["LINK", "USDC"], "top_losers": ["BTC", "ETH", "SOL"],
  "others": {"count": 31, "total": -18.80},
  "degraded_pairs": 0, "identity_error": null,
  "summary": "Giá BTC kéo −$310, ETH −$120. Lãi Earn +$6. Không có nạp/rút."
}
```

`share_of_move = total_i / Σ|total_j|` (dùng tổng trị tuyệt đối để tỷ lệ luôn có nghĩa ngay cả khi
ΔNAV ≈ 0 do tăng và giảm bù nhau).

**Tích hợp chip 24h:** `change_24h` trong `GET /api/portfolio` có thêm `"flow_adjusted_usd"` (sau F2) và
`"has_attribution": true` để UI biết chip có thể chạm vào.

### 2.5 Thiết kế UI

**Điểm vào:** chạm vào chip biến động 24h ở hero → mở **bottom sheet "Vì sao biến động?"** (dùng lại
pattern sheet của Settings: focus trap, Esc, vuốt xuống để đóng). Có thanh chọn cửa sổ
`24H · 7D · 30D` đồng bộ với range của chart.

**Bố cục sheet (mobile-first, từ trên xuống):**

```
┌─────────────────────────────────────────────┐
│ ▼ −$452.00 (−0.88%) · 23.9h                 │  ← dấu + mũi tên + màu (như hero)
│ "Giá BTC kéo −$310, ETH −$120. Lãi Earn +$6."│  ← summary sinh từ payload
├─────────────────────────────────────────────┤
│ CẦU THANG (waterfall rút gọn, bản v2)        │
│ 51,230 ▕██████████████████████████▏ start    │
│  Price   −457.8 ▕▓▓▓▓▏                       │
│  Income    +6.1     ▕▏                       │
│  Flow       0.0                              │
│  Khác      −0.3                              │
│ 50,778 ▕█████████████████████████▏ end       │
├─────────────────────────────────────────────┤
│ TOP CONTRIBUTORS (diverging bar quanh trục 0)│
│ BTC   ◄██████████ −$310.20  giá −1.47%       │
│ ETH       ◄████ −$120.40  giá −2.10%         │
│ SOL          ◄█ −$18.00                      │
│ LINK            █► +$9.30                    │
│ USDC            ▏► +$6.10  số lượng (lãi)    │
│ + 31 tài sản khác  −$18.80                   │
└─────────────────────────────────────────────┘
```

- **Diverging bar** là hiển thị chính vì đọc được tốt trên màn hình hẹp. Mỗi thanh có tooltip/chạm để
  tách price / quantity / interaction. **Waterfall** chỉ hiển thị theo bucket (≤ 6 cột) để không bị chật.
- Vẽ bằng **HTML/CSS** (`div` có `width` theo %), không thêm thư viện chart. Lightweight Charts không có
  waterfall, và nguyên tắc "không CDN, vendored" giữ nguyên.
- Màu: xanh `#22E58B` / đỏ `#FF5C73` như hero, **luôn kèm dấu và mũi tên** (không chỉ dựa vào màu).
  Bar của quantity effect dùng hoa văn sọc hoặc độ đậm thấp hơn để phân biệt với price effect.
- Mọi số USD mang `.priv`. Phần trăm thay đổi giá (không lộ quy mô tài sản) **không** blur.
- Trạng thái: `insufficient_history` → "Cần ít nhất 2 snapshot trong cửa sổ này". `degraded_pairs > 0` →
  banner vàng "N lần đo có nguồn bị lỗi, $X được xếp vào 'thiếu dữ liệu'". `identity_error` → banner đỏ.

### 2.6 Checklist F1

- [ ] **F1.1** `services/analytics/__init__.py` + loader `load_states(db, ids)` (SQL §2.3 bước 3–4) — **P1**
  - AC: trả về state ở grain `(symbol, source)`. ETH on-chain 3 dòng gộp thành 1 key với $Q$ = tổng, $P = V/Q$.
- [ ] **F1.2** Engine phân rã `attribute(states, method, group_by)` thuần, không I/O — **P1**
  - AC1: property test (≥ 200 portfolio ngẫu nhiên) cho thấy $|\sum \text{buckets} - \Delta\text{NAV}| < 10^{-6}$.
  - AC2: test tay: $Q_0=2, P_0=100, Q_1=3, P_1=110$ ⇒ price = 20, quantity = 100, interaction = 10, ΔV = 130.
  - AC3: chuyển 1 ETH Binance → on-chain với giá hai nguồn lệch $0.50 cho price effect gộp của ETH = 0.
  - AC4: các trường hợp biên của §2.2.4 mỗi trường hợp có một test.
- [ ] **F1.3** Hỗ trợ `method=chained|endpoint` — **P1**
  - AC: kịch bản "mua BTC giữa cửa sổ rồi BTC tăng 5%": `chained` dồn lãi vào price, `endpoint` dồn vào interaction. Cả hai đều thoả bất biến.
- [ ] **F1.4** Bucket `revaluation` và `data_gap` (cần P0.2) — **P1**
  - AC: một snapshot có `onchain:arbitrum/main = error` không tạo price effect âm giả cho ETH.
- [ ] **F1.5** Endpoint `GET /api/portfolio/attribution` + validate tham số — **P1**
  - AC: schema §2.4; `window=999d` → 422; không gọi mạng (test bằng fetcher raise); nằm sau token.
- [ ] **F1.6** Sinh câu `summary` tiếng Việt (top 2 price + income + flow) — **P2**
  - AC: không bao giờ nói "không có nạp/rút" khi F2 chưa bật. Khi đó câu là "chưa theo dõi nạp/rút".
- [ ] **F1.7** UI bottom sheet: diverging bar + waterfall bucket + 3 trạng thái — **P1**
  - AC: chạm chip 24h mở sheet; bàn phím (Enter/Esc) dùng được; privacy blur đúng; màn 360px không tràn ngang.
- [ ] **F1.8** v2: tách quantity thành flow/transfer/income/trade_residual (cần F2.6, F2.8) — **P2**
  - AC: rút 1 ETH ra ví ngoài trong cửa sổ cho `flow ≈ −1 × P_ETH`, `trade_residual ≈ 0`.
- [ ] **F1.9** CLI `main.py why [--window 24h] [--json]` — **P3**
  - AC: in cùng số liệu với endpoint, exit 0.

---

## 3. Epic F2: Cash Flow Engine & Internal Transfer Matching

### 3.1 Mục tiêu và phân loại

Mọi thay đổi số lượng phải được gán vào đúng một loại. Đây là tiền đề của TWR (F3) và của câu hỏi
"lãi thật bao nhiêu".

| Loại | Ví dụ | Ảnh hưởng NAV | Ảnh hưởng performance (TWR) |
|---|---|---|---|
| **External deposit** | Nạp EUR→USDT qua P2P, nhận coin từ người khác | ↑ | **Loại trừ** (không phải lãi) |
| **External withdrawal** | Rút về tài khoản ngân hàng, gửi coin cho người khác | ↓ | **Loại trừ** (không phải lỗ) |
| **Internal transfer** | Binance → Ledger, Trust → Binance | ≈ 0 (chỉ mất phí) | Phí tính là chi phí (lỗ thật) |
| **Income** | Lãi Simple Earn, airdrop có giá | ↑ | **Tính** là lợi nhuận |
| **Trade** | Đổi USDT lấy BTC, Convert | ≈ 0 (mất phí) | Không phải flow, nằm trong return |
| **Fee / gas** | Phí rút Binance, gas ETH | ↓ | **Tính** là chi phí |

### 3.2 Kiến trúc module `services/cash_flows.py`

```
                      COLLECTOR PLANE (main.py record, sau Database.record)
┌───────────────────────────────────────────────────────────────────────────────────┐
│ cash_flows.sync(config, db)                                                         │
│                                                                                     │
│  ┌─────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────┐ │
│  │ BinanceFlowSource   │   │ OnchainDeltaDetector      │   │ TxVerifier (P2)      │ │
│  │ deposit/hisrec      │   │ Δq giữa snapshot liên tiếp│   │ eth_getTransaction…  │ │
│  │ withdraw/history    │   │ theo (chain, wallet, sym) │   │ getTransaction (SOL) │ │
│  │ (cursor sync_state) │   │ (cần P0.3)                │   │ xác minh tx_hash     │ │
│  └─────────┬───────────┘   └────────────┬─────────────┘   └──────────┬───────────┘ │
│            └──────────── upsert ────────┴──────────────┬─────────────┘             │
│                                                        ▼                           │
│                                  ┌───────────────────────────────────┐             │
│                                  │ TransferMatcher                    │             │
│                                  │ 1 address · 2 tx_hash · 3 heuristic │            │
│                                  │ → is_internal_transfer, matched_id │             │
│                                  └───────────────────┬───────────────┘             │
└──────────────────────────────────────────────────────┼─────────────────────────────┘
                                                       ▼
                                         portfolio_history.db: cash_flows
                                                       ▲ SELECT
                     PRESENTATION: /api/cash-flows, attribution v2, analytics F3
```

- `sync` được gọi **sau** khi snapshot đã commit. Lỗi của `sync` chỉ ghi stderr, **không bao giờ** làm
  hỏng việc ghi snapshot (cùng tinh thần graceful degradation hiện có).
- Lõi matching là **hàm thuần** `match(flows: list[Flow], owned: OwnedAddresses, cfg) -> list[Match]`
  để test không cần mạng hay DB.
- `api_gateway.py` không đọc bảng này. Gateway giữ đúng một route.

### 3.3 Schema database (migration v4)

```sql
CREATE TABLE IF NOT EXISTS cash_flows (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,     -- ISO8601 UTC: completeTime (Binance) / block time / cận phải cửa sổ
    window_start          TEXT,                 -- chỉ cho flow suy ra từ delta snapshot: flow nằm trong (window_start, timestamp]
    source                TEXT    NOT NULL,     -- binance | evm:<chain> | solana
    account               TEXT    NOT NULL,     -- 'binance' hoặc địa chỉ ví (lowercase với EVM)
    account_label         TEXT    NOT NULL DEFAULT '',
    asset                 TEXT    NOT NULL,     -- symbol chuẩn hoá (khớp snapshot_assets.symbol)
    network               TEXT    NOT NULL DEFAULT '',  -- ETH | BSC | ARBITRUM | BASE | MATIC | SOL (tên Binance)
    direction             TEXT    NOT NULL CHECK (direction IN ('in','out')),
    amount                REAL    NOT NULL CHECK (amount >= 0),   -- luôn dương, dấu nằm ở direction
    fee_amount            REAL    NOT NULL DEFAULT 0,
    fee_asset             TEXT    NOT NULL DEFAULT '',
    price_usd             REAL    NOT NULL DEFAULT 0,  -- giá tại thời điểm flow (snapshot gần nhất trước đó)
    value_usd             REAL    NOT NULL DEFAULT 0,  -- amount × price_usd, luôn dương
    flow_type             TEXT    NOT NULL,     -- deposit | withdrawal | reward | airdrop | fee | adjustment
    status                TEXT    NOT NULL DEFAULT 'completed',  -- pending | completed | failed
    tx_hash               TEXT    NOT NULL DEFAULT '',
    counterparty_address  TEXT    NOT NULL DEFAULT '',  -- địa chỉ đích (withdraw) / nguồn (nếu biết)
    is_internal_transfer  INTEGER NOT NULL DEFAULT 0,
    matched_flow_id       INTEGER REFERENCES cash_flows(id) ON DELETE SET NULL,
    match_method          TEXT    NOT NULL DEFAULT '',  -- address | tx_hash | heuristic | manual
    match_confidence      REAL    NOT NULL DEFAULT 0,   -- [0, 1]
    detection_method      TEXT    NOT NULL,     -- binance_api | snapshot_delta | tx_verify | manual
    classified_by         TEXT    NOT NULL DEFAULT 'auto',  -- auto | user  (auto KHÔNG ghi đè user)
    snapshot_id_before    INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    snapshot_id_after     INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    external_id           TEXT    NOT NULL,     -- id Binance, hoặc '<chain>:<wallet>:<asset>:<snap_after>' cho delta
    raw_json              TEXT    NOT NULL DEFAULT '{}',
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    note                  TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cash_flows_ext   ON cash_flows(source, flow_type, external_id);
CREATE INDEX        IF NOT EXISTS idx_cash_flows_ts   ON cash_flows(timestamp);
CREATE INDEX        IF NOT EXISTS idx_cash_flows_tx   ON cash_flows(tx_hash) WHERE tx_hash <> '';
CREATE INDEX        IF NOT EXISTS idx_cash_flows_asset ON cash_flows(asset, timestamp);

CREATE TABLE IF NOT EXISTS sync_state (       -- con trỏ đồng bộ tăng dần
    key        TEXT PRIMARY KEY,              -- 'binance.deposit', 'binance.withdraw', 'binance.earn_rewards'
    value      TEXT NOT NULL,                 -- epoch ms của lần đồng bộ thành công cuối
    updated_at TEXT NOT NULL
);
```

**Quy ước dấu dùng trong analytics:** $F = +\text{value\_usd}$ nếu `direction = in`, $-\text{value\_usd}$
nếu `out`. **Dòng tiền ngoài** (external) của danh mục là:

```sql
SELECT timestamp, CASE direction WHEN 'in' THEN value_usd ELSE -value_usd END AS f
FROM cash_flows
WHERE is_internal_transfer = 0 AND status = 'completed' AND flow_type IN ('deposit','withdrawal');
```

Hai vế của một internal transfer có tổng ≈ −phí và bị loại khỏi dòng tiền ngoài. Phí được giữ trong
return (đúng bản chất: đó là tiền mất thật).

**Idempotency:** mọi ghi là `INSERT … ON CONFLICT(source, flow_type, external_id) DO UPDATE` chỉ trên
các cột trạng thái (`status`, `tx_hash`, `timestamp`), **không** đụng tới các cột phân loại khi
`classified_by = 'user'`.

### 3.4 Tích hợp Binance API (read-only)

| Endpoint | Dùng cho | Tham số chính | Trường dùng |
|---|---|---|---|
| `GET /sapi/v1/capital/deposit/hisrec` | Nạp vào Binance | `startTime`, `endTime` (**cửa sổ tối đa 90 ngày**), `limit ≤ 1000`, `offset`, `status` | `id`, `coin`, `amount`, `network`, `status`, `address`, `txId`, `insertTime`, `completeTime`, `transferType` |
| `GET /sapi/v1/capital/withdraw/history` | Rút khỏi Binance | như trên (cửa sổ tối đa 90 ngày) | `id`, `coin`, `amount`, `transactionFee`, `network`, `status`, `address`, `txId`, `applyTime`, `completeTime`, `transferType` |
| `GET /sapi/v1/simple-earn/flexible/history/rewardsRecord` (F2.8) | Lãi Earn (income) | `type`, `startTime`, `endTime` | `asset`, `rewards`, `time` |
| `GET /sapi/v1/asset/assetDividend` (F2.8) | Airdrop, phân phối | `startTime`, `endTime`, `limit` | `asset`, `amount`, `divTime`, `enInfo` |

> ⚠️ **[cần kiểm chứng khi triển khai]** với tài liệu Binance hiện hành: (1) mã `status` (giả định:
> deposit `1 = success`, `0/6/8 = pending`; withdraw `6 = completed`, `1/3/5 = cancelled/rejected/failure`);
> (2) `withdraw.amount` đã trừ `transactionFee` hay chưa; (3) weight của từng endpoint (withdraw history
> có UID weight cao, nên **không** gọi ở mỗi request UI, chỉ gọi trong collector); (4) định dạng
> `applyTime` (chuỗi `"YYYY-MM-DD HH:MM:SS"` UTC, khác với epoch ms của deposit).

**Quy tắc đồng bộ:**
1. Lần đầu: backfill lùi về `cash_flows.backfill_days` (mặc định 365), chia thành các khúc ≤ 90 ngày.
2. Các lần sau: từ `sync_state.value − 2 ngày` (chồng lấn để bắt các bản ghi đổi trạng thái
   pending → completed) tới `now`. Upsert giúp chồng lấn không tạo bản ghi trùng.
3. Dùng lại transport `_request` (ký HMAC, failover host, retry, đồng bộ đồng hồ). **Không thêm endpoint
   ghi nào.** Key vẫn chỉ cần *Enable Reading*.
4. `transferType = 1` (chuyển nội bộ off-chain giữa các tài khoản Binance) vẫn là flow **external** đối
   với danh mục (tiền rời khỏi tài khoản của Jean), trừ khi người dùng đánh dấu khác.
5. Nút "Test" trong Settings kiểm tra thêm khả năng đọc `deposit/hisrec` (một request `limit=1`) và báo
   chip "History ✓/✗".

### 3.5 Phát hiện delta on-chain (cần P0.3)

Các ví self-custody không có "lịch sử nạp/rút" có sẵn như Binance. v1 suy ra flow từ thay đổi số dư giữa
hai snapshot.

```text
Với mỗi cặp snapshot liên tiếp (a, b), mỗi key = (chain, wallet, symbol):
   Δq = q_b − q_a
   P  = p_a nếu p_a > 0, ngược lại p_b
   bỏ qua nếu nguồn (chain, wallet) lỗi ở a hoặc b                    # P0.2
   bỏ qua nếu |Δq|·P < min_flow_usd (mặc định $10)                   # nhiễu, dust
   nếu symbol là native coin của chain và Δq < 0 và |Δq|·P < gas_max_usd ($5):
        ghi flow_type = 'fee' (gas)                                  # tiêu gas, không phải rút
        tiếp
   nếu symbol ∈ cash_flows.ignore_symbols (spam có giá): bỏ qua
   ghi cash_flow: direction = in nếu Δq > 0, out nếu Δq < 0, amount = |Δq|,
                  window_start = ts_a, timestamp = ts_b, detection_method = 'snapshot_delta',
                  external_id = f"{chain}:{wallet}:{symbol}:{b}"
```

**Giới hạn đã biết của v1** (ghi rõ để không ai tin nhầm vào độ chính xác):
- Độ phân giải thời gian bằng nhịp snapshot (1h sau P0.5). Thời điểm flow chỉ được biết là thuộc
  khoảng `(window_start, timestamp]`.
- Nạp và rút cùng một tài sản trong cùng khoảng sẽ bù trừ nhau và không thấy được.
- Swap on-chain (USDC → ETH trên Uniswap) hiện thành một `out` USDC và một `in` ETH. Matcher bước 4
  (§3.6) nhận diện cặp này là **trade** (cùng ví, cùng khoảng, giá trị ≈ nhau, khác tài sản) và đánh dấu
  `flow_type = 'adjustment'`, không phải flow ngoài.
- **v2 (F2.9, P2):** xác minh mức giao dịch bằng `tx_hash` từ Binance (§3.6 bước 2). Sau đó có thể
  dùng explorer API (Etherscan V2 đa chain, một key, hoặc Solana `getSignaturesForAddress`) để có lịch
  sử tx đầy đủ. Bước này thêm một bí mật mới (tier T1-bis, chỉ đọc) và phải được ghi vào §4.1 kiến trúc.

### 3.6 Thuật toán đối soát chuyển khoản nội bộ (Internal Transfer Matching)

Bốn tầng, chạy theo thứ tự. Tầng trước khớp được thì tầng sau bỏ qua cặp đó. Mỗi flow khớp **tối đa một**
flow đối ứng.

| Tầng | Điều kiện | `match_method` | Confidence |
|---|---|---|---|
| **1. Address** | Binance withdrawal có `address` (lowercase với EVM) ∈ tập địa chỉ ví đã cấu hình **và** `network` ánh xạ đến một chain mà ví đó theo dõi | `address` | 1.00 |
| **2. Tx hash** | Binance deposit có `txId`, tra on-chain (`eth_getTransactionByHash` / Solana `getTransaction`) thấy `from` (hoặc người gửi trong log `Transfer` với ERC-20) ∈ ví sở hữu; hoặc `txId` trùng `tx_hash` của một flow khác | `tx_hash` | 1.00 |
| **3. Heuristic** | Xem công thức bên dưới | `heuristic` | $s \in [0.6, 1)$ |
| **4. Swap (cùng ví)** | Cặp `out`/`in` khác tài sản, cùng `(source, account)`, cùng khoảng snapshot, $\lvert V_{out} - V_{in}\rvert / V_{out} < 3\%$ | `heuristic` | 0.7, `flow_type = adjustment` |

**Tầng 3: heuristic theo số lượng + thời gian.** Hai flow $o$ (out) và $i$ (in) là ứng viên nếu:

1. Cùng `asset` sau chuẩn hoá (bảng alias: `WETH→ETH`, `BTCB→BTC`… chỉ khi người dùng bật), và
   `account` khác nhau (Binance ↔ ví, hoặc ví ↔ ví).
2. **Thời gian chồng lấn:** gọi $I_o$, $I_i$ là khoảng thời gian của mỗi flow. Flow từ Binance API là
   một điểm $T$ nên $I = [T, T]$. Flow từ delta snapshot là $I = (\text{window\_start}, \text{timestamp}]$.
   Điều kiện:
   $$[\,\min I_o - 30\text{m},\ \max I_o + 30\text{m}\,] \ \cap\ I_i \neq \varnothing$$
   Với flow dạng điểm ở cả hai phía, điều này đúng bằng quy tắc $T_i \in [T_o - 30\text{m}, T_o + 30\text{m}]$
   của brief. Với flow suy ra từ snapshot, quy tắc tự nới theo độ rộng khoảng snapshot (không thể khớp
   chính xác hơn độ phân giải của dữ liệu).
3. **Số lượng:** $a_i \le a_o$ và
   $$\frac{a_o - a_i - \text{fee}_o}{a_o} \in [-\varepsilon, \varepsilon_{\text{fee}}]$$
   với $\varepsilon = 0.5\%$ (`amount_tolerance_pct`) và $\varepsilon_{\text{fee}}$ = max(0.5%, phí rút
   tối đa hợp lý của mạng / $a_o$). Như vậy chấp nhận cả trường hợp Binance đã trừ phí lẫn chưa trừ.

**Điểm số** của ứng viên:

$$s = 1 - 0.25\cdot\min\!\left(1, \frac{|a_o - \text{fee}_o - a_i|}{\varepsilon\, a_o}\right) - 0.25\cdot\min\!\left(1, \frac{d(I_o, I_i)}{30\text{m}}\right) \in [0.5, 1]$$

(mỗi thành phần đóng góp tối đa 0.25 điểm phạt). $d$ là khoảng cách giữa hai khoảng, bằng 0 khi chúng
giao nhau. Chỉ chấp nhận $s \ge 0.6$.

**Gán một-một:** sắp mọi cặp ứng viên theo $s$ giảm dần, duyệt tham lam, bỏ cặp nào có một vế đã được
dùng. Với số flow cỡ chục mỗi ngày, tham lam là đủ (không cần thuật toán Hungarian).

**Khi khớp:** cả hai bản ghi có `is_internal_transfer = 1`, `matched_flow_id` trỏ chéo nhau, cùng
`match_method` và `match_confidence`. Chênh lệch $a_o - a_i$ (định giá USD) được ghi thành một dòng
`flow_type = fee` để phí xuất hiện trong attribution (§2.2.5, bucket `transfer`).

**Đang trên đường (in transit):** Binance withdrawal đã `completed` nhưng chưa có `in` khớp, và tuổi < 2h:
giữ `is_internal_transfer = 0`, `status = 'pending'` và **chưa** coi là flow ngoài. Sau 2h vẫn không khớp
thì chốt là external withdrawal (người dùng có thể sửa tay). Nhờ vậy NAV tạm sụt lúc coin đang bay không
bị TWR hiểu nhầm.

**Can thiệp thủ công (F2.10):** `PATCH /api/cash-flows/{id}` với `{"is_internal_transfer": true,
"matched_flow_id": 42, "note": "…"}` đặt `classified_by = 'user'`. Mọi lần sync sau đều tôn trọng quyết
định này.

### 3.7 Endpoint đọc

```
GET /api/cash-flows?days=90&type=external|internal|income|all&asset=ETH
200 {
  "count": 3,
  "net_external_usd": -1200.00,
  "flows": [{
    "id": 17, "timestamp": "…", "source": "binance", "asset": "ETH", "direction": "out",
    "amount": 0.5, "value_usd": 1750.00, "flow_type": "withdrawal",
    "is_internal_transfer": true, "matched_flow_id": 18, "match_method": "address",
    "match_confidence": 1.0, "tx_hash": "0xabc…", "counterparty_label": "My Ledgy"
  }],
  "unmatched_pending": 0, "last_sync": {"binance.deposit": "…", "binance.withdraw": "…"}
}
```

UI: tab "Dòng tiền" trong sheet F1, liệt kê flow theo ngày. Internal transfer hiện thành **một dòng** có
mũi tên `Binance → My Ledgy`. Flow `heuristic` có confidence < 0.9 mang nhãn "đoán, chạm để xác nhận".
`tx_hash` và địa chỉ được rút gọn (`0xabc…1234`) và có `.priv`.

### 3.8 Cấu hình (`config.yaml`)

```yaml
cash_flows:
  enabled: true
  backfill_days: 365
  match_window_minutes: 30
  amount_tolerance_pct: 0.5
  in_transit_hours: 2
  min_flow_usd: 10           # ngưỡng phát hiện delta on-chain
  gas_max_usd: 5             # giảm native coin nhỏ hơn mức này = gas
  ignore_symbols: []         # token spam có giá
  asset_aliases: {}          # vd { WETH: ETH, BTCB: BTC } — mặc định tắt
```

> `POST /api/settings` đã giữ nguyên các key top-level không quen (`read_raw_config` rồi merge). Tuy
> nhiên ghi config **làm mất comment YAML** (§2.1.3 kiến trúc). `config.py` phải được mở rộng để parse
> và validate block này, với default an toàn khi thiếu.

### 3.9 Checklist F2

- [ ] **F2.1** Migration v4: `cash_flows`, `sync_state`, các index (cần P0.1) — **P1**
  - AC: CHECK constraint chặn `amount < 0` và `direction` lạ; insert trùng `(source, flow_type, external_id)` được upsert, không nhân bản.
- [ ] **F2.2** `BinanceClient.deposit_history()`, `withdraw_history()` + phân khúc 90 ngày + phân trang — **P1**
  - AC: backfill 365 ngày sinh đúng 5 khúc; mock trả 1000 dòng thì gọi trang tiếp; chỉ dùng phương thức GET và path đọc (test liệt kê mọi URL đã gọi).
- [ ] **F2.3** `cash_flows.sync()` + con trỏ `sync_state` + chồng lấn 2 ngày, nối vào `main.py record` — **P1**
  - AC: chạy hai lần liên tiếp không sinh dòng mới; Binance lỗi 401 thì snapshot vẫn được ghi và lỗi ra stderr; pending → completed được cập nhật.
- [ ] **F2.4** Chuẩn hoá: `network` Binance ↔ chain nội bộ (`ETH↔ethereum`, `BSC↔bsc`, `ARBITRUM↔arbitrum`, `BASE↔base`, `MATIC/POL↔polygon`, `SOL↔solana`), địa chỉ EVM lowercase, thời gian ISO UTC — **P1**
  - AC: test bảng ánh xạ; network lạ thì giữ nguyên và không khớp tầng 1.
- [ ] **F2.5** `OnchainDeltaDetector` (cần P0.3) — **P1**
  - AC: Δ < `min_flow_usd` bị bỏ; gas native nhỏ thành `fee`; nguồn lỗi bị bỏ qua; `external_id` ổn định nên chạy lại không trùng.
- [ ] **F2.6** `TransferMatcher` tầng 1 (address) + tầng 3 (heuristic) + gán một-một + dòng phí — **P1**
  - AC1: Binance rút 1.000 ETH lúc T (fee 0.0004), ví nhận 0.9996 ETH trong khoảng snapshot chứa T+12m → `is_internal_transfer=1` cả hai vế, confidence ≥ 0.9, sinh một dòng `fee`.
  - AC2: hai lần rút 0.5 ETH cách nhau 10 phút, hai lần nhận tương ứng → hai cặp riêng, không vế nào dùng hai lần.
  - AC3: ví nhận 1 ETH mà không có lệnh rút Binance tương ứng → external deposit.
  - AC4: NAV tổng giữa hai snapshot bao quanh một internal transfer có dòng tiền ngoài = 0.
- [ ] **F2.7** Trạng thái in-transit + chốt sau `in_transit_hours` — **P1**
  - AC: snapshot chụp lúc coin đang bay không tạo flow ngoài; sau 2h không khớp thì thành `withdrawal` external.
- [ ] **F2.8** Income: Simple Earn rewards + asset dividend → `flow_type = reward | airdrop` — **P2**
  - AC: tổng `reward` USDC trong 7 ngày khớp với tăng số lượng USDC trong Earn (sai lệch < 1%) khi không có flow khác.
- [ ] **F2.9** `TxVerifier`: tầng 2 (tx_hash) qua RPC đã cấu hình; tầng 4 (swap cùng ví) — **P2**
  - AC: deposit Binance có `txId` mà `from` là ví của Jean → internal, confidence 1.0, kể cả khi số lượng lệch > ε.
- [ ] **F2.10** `GET /api/cash-flows` + `PATCH /api/cash-flows/{id}` (override thủ công) — **P2**
  - AC: override đặt `classified_by=user`; lần sync sau không ghi đè; endpoint nằm sau token.
- [ ] **F2.11** UI tab "Dòng tiền" + nhãn "đoán, chạm để xác nhận" — **P2**
  - AC: internal transfer hiện một dòng `A → B`; privacy blur cho số tiền, hash, địa chỉ.
- [ ] **F2.12** `change_24h.flow_adjusted_usd` trong `GET /api/portfolio` + chip hiện "(đã trừ nạp/rút)" — **P2**
  - AC: nạp $1000 trong 24h, thị trường đứng yên → chip thô +$1000, điều chỉnh ≈ $0.
- [ ] **F2.13** Settings → Test kiểm tra quyền đọc lịch sử nạp/rút — **P3**
- [ ] **F2.14** Lịch sử tx on-chain đầy đủ qua explorer API (thêm secret, cập nhật §4.1 kiến trúc) — **P3**

---

## 4. Epic F3: Quantitative Portfolio Intelligence

### 4.0 Kiến trúc chung

```
services/analytics/
├── __init__.py
├── attribution.py     F1
├── series.py          chuỗi NAV ngày + dòng tiền ngày + return ngày (nền của mọi module dưới)
├── returns.py         TWR, MWR (XIRR), PnL theo kỳ
├── benchmark.py       đồng bộ giá benchmark (collector) + đọc từ DB (presentation)
├── risk.py            MDD/underwater, volatility, Sharpe, Sortino, VaR/CVaR
├── allocation.py      tỷ trọng mục tiêu, drift, lệnh gợi ý
└── concentration.py   HHI, CR3, CEX vs self-custody
```

- **Chỉ dùng numpy** (đã có sẵn qua matplotlib) và thư viện chuẩn. **Không thêm pandas/scipy** (decision D2).
- Mọi hàm tính toán là **hàm thuần** nhận mảng/dataclass. I/O (SQLite, HTTP) nằm ở lớp mỏng bên ngoài.
- Endpoint đều là `GET`, không gọi mạng, nằm sau token. Payload luôn có `n_obs`, `sufficient`,
  `as_of`, `period`.

| Endpoint | Module |
|---|---|
| `GET /api/analytics/performance?period=30d` | `returns.py` (TWR, MWR, PnL) |
| `GET /api/analytics/pnl` | `returns.py` (bảng 7d/30d/90d/YTD/ALL) |
| `GET /api/analytics/benchmark?vs=BTC,ETH,SPX&period=90d` | `benchmark.py` + `risk.py` |
| `GET /api/analytics/risk?period=90d` | `risk.py` |
| `GET /api/analytics/allocation` | `allocation.py` |
| `GET /api/analytics/concentration` | `concentration.py` |

**Cấu hình chung:**

```yaml
analytics:
  day_boundary_tz: UTC          # ranh giới ngày, khớp với nến ngày của benchmark crypto
  risk_free_rate: 0.04          # năm, USD
  min_observations: 30          # dưới mức này: sufficient=false, UI hiện "cần thêm dữ liệu"
  max_close_staleness_hours: 6  # snapshot "đóng cửa ngày" phải cách 00:00 UTC không quá 6h
  benchmarks: [BTC, ETH, SPX]
```

> ⚠️ **Thực tế dữ liệu:** hôm nay mới có 3 ngày lịch sử. Không thể dựng lại NAV quá khứ một cách đáng
> tin (trừ khi tái tạo số dư từ toàn bộ lịch sử giao dịch Binance, ngoài phạm vi). Vì vậy **thứ tự giao
> hàng của F3 đi theo lượng dữ liệu cần**: Concentration và Allocation (chỉ cần snapshot hiện tại) làm
> trước; TWR/PnL hữu ích sau ~1 tuần; Volatility/Sharpe/Beta cần ≥ 30 ngày; VaR cần ≥ 60 ngày (ổn định
> từ ~250). Đây là lý do P0.5 phải làm **ngay hôm nay**: mỗi ngày trì hoãn là một ngày dữ liệu mất vĩnh viễn.

### 4.1 Nền: chuỗi ngày (`series.py`)

#### 4.1.1 NAV đóng cửa ngày

Với ngày $d$ (theo `day_boundary_tz`), $\text{close}_d$ = snapshot **hợp lệ** (không có nguồn lỗi,
P0.2) mới nhất có timestamp $\le$ 24:00 của ngày $d$ và $\ge$ 24:00 − `max_close_staleness_hours`. Nếu
không có thì ngày $d$ là **gap**.

#### 4.1.2 Return ngày đã khử dòng tiền (Modified Dietz theo ngày)

Gọi $t_{d-1}$, $t_d$ là timestamp thật của hai snapshot đóng cửa, $F_j$ là các dòng tiền ngoài (có dấu,
§3.3) có timestamp trong $(t_{d-1}, t_d]$, và trọng số thời gian còn lại:

$$w_j = \frac{t_d - t_j}{t_d - t_{d-1}} \in [0, 1)$$

$$r_d = \frac{V_d - V_{d-1} - \sum_j F_j}{V_{d-1} + \sum_j w_j F_j}$$

- Không có flow thì $r_d = V_d / V_{d-1} - 1$.
- Mẫu số $\le$ $1 hoặc $\le$ 1% của $V_{d-1}$ (ví dụ rút gần hết): ghi `r_d = null`, cờ `unstable`.
- Gap $g$ ngày: return giữa hai close hợp lệ được giữ thành **một** quan sát, cờ `gap_days = g`. Quan sát
  này **được dùng** cho TWR, MDD, PnL (đại lượng mức/đường đi) nhưng **bị loại** khỏi σ, Sharpe, Sortino,
  Beta, VaR (các đại lượng giả định mỗi quan sát là 1 ngày).
- Trước khi F2 xong: $F_j = 0$ và payload ghi `"flow_adjusted": false`. UI hiện cảnh báo
  "chưa khử nạp/rút".

#### 4.1.3 Chỉ số TWR (Base 100)

$$I_0 = 100, \qquad I_d = I_{d-1}\,(1 + r_d)$$

$I$ là đường cong "tăng trưởng của $100 đầu tư", **không bị nạp/rút làm méo**. MDD, underwater chart và
benchmark đều dùng $I$, **không** dùng NAV thô. Nếu dùng NAV, một lần rút tiền sẽ hiện thành drawdown.

### 4.2 Module TWR & MWR (`returns.py`)

#### 4.2.1 TWR (Time-Weighted Return)

$$\text{TWR}_{[0,T]} = \prod_{d=1}^{T} (1 + r_d) - 1 = \frac{I_T}{I_0} - 1$$

Annualize **chỉ khi** kỳ ≥ 365 ngày (theo chuẩn GIPS, không annualize kỳ ngắn hơn 1 năm):

$$\text{TWR}_{\text{ann}} = (1 + \text{TWR})^{365/T} - 1$$

TWR đo **kỹ năng chọn tài sản**: không phụ thuộc vào việc Jean nạp thêm tiền lúc nào.

#### 4.2.2 MWR (Money-Weighted Return, XIRR)

Từ góc nhìn nhà đầu tư, dòng tiền là: $c_0 = -V_0$ tại $t_0$; mỗi flow ngoài $c_j = -F_j$ (nạp là tiền
bỏ vào nên âm, rút là tiền lấy ra nên dương); $c_T = +V_T$. MWR là $r$ thoả:

$$\text{NPV}(r) = \sum_j c_j\,(1 + r)^{-\tau_j} = 0, \qquad \tau_j = \frac{t_j - t_0}{365\text{ ngày}}$$

- Solver: **Newton–Raphson** với đạo hàm giải tích $\text{NPV}'(r) = \sum_j -\tau_j c_j (1+r)^{-\tau_j-1}$,
  khởi tạo bằng TWR năm hoá. Không hội tụ sau 50 vòng hoặc nhảy ra ngoài $(-0.9999, 100)$ thì chuyển sang
  **bisection** trên $[-0.9999, 100]$. Không có đổi dấu thì trả `null` kèm `reason`.
- Kỳ < 1 năm: trả cả `mwr_annualized` và `mwr_period = (1 + r)^{\tau_T} - 1` (UI mặc định hiện con số
  theo kỳ cho khớp TWR).
- **Diễn giải trên UI:** $\text{MWR} > \text{TWR}$ nghĩa là Jean nạp tiền đúng lúc (timing tốt), và
  ngược lại.

#### 4.2.3 PnL theo kỳ

Với kỳ $p \in \{7d, 30d, 90d, \text{YTD}, \text{ALL}\}$, $t_s$ = close của ngày bắt đầu kỳ (YTD: close
ngày 31/12 năm trước, hoặc snapshot đầu tiên nếu lịch sử ngắn hơn):

| Trường | Công thức | Ý nghĩa |
|---|---|---|
| `nav_change_usd` | $V_T - V_s$ | Như chip 24h hiện nay (thô) |
| `net_flows_usd` | $\sum F_j$ trong $(t_s, T]$ | Tiền nạp/rút ròng |
| `pnl_usd` | $V_T - V_s - \sum F_j$ | **Lãi/lỗ thật** bằng USD |
| `twr_pct` | $I_T / I_s - 1$ | Hiệu suất % không bị flow làm méo |
| `mwr_pct` | §4.2.2 trên kỳ | Hiệu suất % theo tiền của Jean |
| `coverage_days` | số ngày thật có dữ liệu | Kỳ 90d mà chỉ có 12 ngày thì UI ghi "12d" |

### 4.3 Benchmark: BTC, ETH, S&P 500 (`benchmark.py`)

#### 4.3.1 Nguồn dữ liệu

| Benchmark | Nguồn chính | Dự phòng | Ghi chú |
|---|---|---|---|
| **BTC** | Binance `GET /api/v3/klines?symbol=BTCUSDT&interval=1d` qua `data-api.binance.vision` | CoinGecko `/coins/bitcoin/market_chart` | Không cần key. Dùng lại host public client đang dùng. Nến đóng lúc 00:00 UTC, khớp `day_boundary_tz`. |
| **ETH** | Binance klines `ETHUSDT` | CoinGecko `/coins/ethereum/market_chart` | như trên |
| **SPX** | Yahoo Finance `query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d` | Stooq CSV `stooq.com/q/d/l/?s=^spx&i=d` | Cả hai **không chính thức**, có thể đổi bất cứ lúc nào. Thất bại thì SPX hiện "không có dữ liệu", không chặn BTC/ETH. |

Chọn Binance cho BTC/ETH thay vì CoinGecko vì: không cần key (CoinGecko public API giới hạn rate và lịch
sử), nhất quán với nguồn định giá của chính danh mục, và host `data-api.binance.vision` đã được chứng
minh hoạt động trên máy này (decision D3).

**Lưu trữ (migration v5):**

```sql
CREATE TABLE IF NOT EXISTS benchmark_prices (
    symbol  TEXT NOT NULL,           -- BTC | ETH | SPX
    date    TEXT NOT NULL,           -- YYYY-MM-DD (ngày đóng cửa theo UTC; SPX: ngày giao dịch New York)
    close   REAL NOT NULL,
    source  TEXT NOT NULL,           -- binance | coingecko | yahoo | stooq
    PRIMARY KEY (symbol, date)
);
```

Collector (`main.py record`, tối đa một lần/ngày cho mỗi symbol, hoặc lệnh riêng
`main.py sync-benchmarks`) upsert dữ liệu. Endpoint chỉ đọc bảng này.

#### 4.3.2 Chuẩn hoá Base 100

$$B_d = 100 \cdot \frac{P^{b}_d}{P^{b}_{s}}, \qquad I^{p}_d = 100 \cdot \frac{I_d}{I_s}$$

Cả hai bắt đầu ở 100 tại ngày đầu kỳ $s$. Danh mục dùng **chỉ số TWR** (§4.1.3), không dùng NAV.

#### 4.3.3 Căn lịch (calendar alignment)

Crypto giao dịch 365 ngày, S&P 500 khoảng 252 ngày/năm.

- **BTC/ETH:** dùng chung lịch ngày UTC với danh mục. Hệ số năm hoá $A = 365$.
- **SPX:** tính return danh mục **giữa các ngày giao dịch SPX liên tiếp** ($r^p_{[d_{k-1}, d_k]} =
  I_{d_k}/I_{d_{k-1}} - 1$; return thứ Sáu → thứ Hai gộp cả cuối tuần). Hệ số năm hoá $A = 252$. Close
  SPX là 16:00 New York ≈ 21:00 UTC, lệch với close danh mục 00:00 UTC; chấp nhận độ lệch 3h này và ghi
  chú trong payload (`"alignment": "spx_trading_days, ~3h close offset"`).

#### 4.3.4 Công thức thống kê

Trên $n$ cặp return đã căn lịch $(r^p_k, r^b_k)$, với lãi suất phi rủi ro mỗi kỳ
$r_f = (1 + R_f)^{1/A} - 1$:

| Chỉ số | Công thức | Ghi chú |
|---|---|---|
| **Beta** | $\beta = \dfrac{\operatorname{Cov}(r^p, r^b)}{\operatorname{Var}(r^b)}$ | `ddof=1` cho cả hai |
| **Pearson correlation** | $\rho = \dfrac{\operatorname{Cov}(r^p, r^b)}{\sigma_p\,\sigma_b}$ | $\in [-1, 1]$ |
| **Tracking Error** | $\text{TE} = \sigma(r^p - r^b) \cdot \sqrt{A}$ | năm hoá |
| **Jensen's Alpha** | $\alpha_{\text{ann}} = A \cdot \left[\,\overline{r^p - r_f} - \beta\,\overline{r^b - r_f}\,\right]$ | alpha năm hoá theo dạng số học |
| **Information Ratio** (bonus) | $\text{IR} = \dfrac{A \cdot \overline{r^p - r^b}}{\text{TE}}$ | |
| **Excess return kỳ** | $(I^p_T / 100) - (B_T / 100)$ | điểm % trên chart |

`sufficient = n ≥ min_observations` (30). Dưới ngưỡng: vẫn trả base-100 chart (luôn có ý nghĩa) nhưng
`beta`, `correlation`, `tracking_error`, `alpha` là `null`.

**Bối cảnh danh mục hiện tại:** ~93% là Earn, trong đó có nhiều stablecoin. Beta so với BTC sẽ thấp
một cách tự nhiên. Đó là thông tin đúng, không phải lỗi.

### 4.4 Risk & Volatility Engine (`risk.py`)

Mọi chỉ số dùng $r_d$ đã khử dòng tiền (§4.1.2), loại quan sát có cờ `gap_days > 1` hoặc `unstable`
(trừ MDD, vốn dùng $I$). $A = 365$.

#### 4.4.1 Max Drawdown & Underwater

$$\text{HWM}_d = \max_{s \le d} I_s, \qquad \text{DD}_d = \frac{I_d}{\text{HWM}_d} - 1 \le 0, \qquad \text{MDD} = \min_d \text{DD}_d$$

Trả thêm: ngày đỉnh, ngày đáy, ngày hồi phục (lần đầu $I_d \ge \text{HWM}_{\text{peak}}$, `null` nếu chưa),
`duration_days` (đỉnh → hồi phục), `current_drawdown`. **Underwater chart:** chuỗi $\text{DD}_d$ vẽ bằng
`BaselineSeries` của Lightweight Charts (baseline 0, vùng âm tô đỏ), dùng chung trục thời gian với chart
NAV. Với MDD có thể dùng thêm điểm intraday (mọi snapshot) để không bỏ sót đáy giữa ngày. Ghi rõ
`"resolution": "daily" | "snapshot"`.

#### 4.4.2 Volatility năm hoá

$$\sigma_{\text{daily}} = \sqrt{\frac{1}{n-1}\sum_d (r_d - \bar r)^2}, \qquad \sigma_{\text{annual}} = \sigma_{\text{daily}} \times \sqrt{365}$$

Bonus: rolling 30d σ (chuỗi) để vẽ.

#### 4.4.3 Sharpe Ratio

$$S = \frac{R_p - R_f}{\sigma_p} = \frac{\overline{r_d - r_f}}{\sigma(r_d - r_f)} \cdot \sqrt{365}, \qquad r_f = (1+R_f)^{1/365} - 1$$

Dạng thứ hai là cùng công thức sau khi năm hoá tử ($\times 365$) và mẫu ($\times\sqrt{365}$). Dùng
dạng này để tránh trộn lẫn return hình học và số học.

#### 4.4.4 Sortino Ratio

Downside deviation tính trên **toàn bộ** $n$ quan sát (chỉ các quan sát dưới MAR mới đóng góp khác 0).
Lỗi phổ biến là chỉ lấy std của các return âm, và ở đây phải tránh lỗi đó:

$$\text{DD}_{\text{down}} = \sqrt{\frac{1}{n}\sum_{d=1}^{n} \min(0,\ r_d - \text{MAR})^2}, \qquad \text{Sortino} = \frac{\overline{r_d - \text{MAR}}}{\text{DD}_{\text{down}}}\cdot\sqrt{365}$$

MAR mặc định bằng $r_f$ theo ngày. $\text{DD}_{\text{down}} = 0$ (không có ngày nào dưới MAR) thì trả
`null`, `reason: "no_downside"`, không trả `inf`.

#### 4.4.5 Historical VaR 95% (1 ngày) và CVaR

$$q_{5\%} = \text{Quantile}_{0.05}\big(\{r_d\}\big), \qquad \text{VaR}_{95\%}^{\$} = -\,q_{5\%} \times V_T$$

$$\text{CVaR}_{95\%}^{\$} = -\,\overline{\{r_d : r_d \le q_{5\%}\}} \times V_T$$

- `np.quantile(r, 0.05, method="lower")`: chọn quan sát thực tế thấp hơn thay vì nội suy (thận trọng
  hơn, và đúng nghĩa "historical").
- Diễn giải trên UI: "Trong 95% số ngày, danh mục không mất quá **$X** (Y%)." VaR luôn đi kèm CVaR
  ("ngày xấu nhất 5% mất trung bình $Z").
- `n < 60` → `null`; `60 ≤ n < 250` → trả giá trị kèm `"reliability": "low"`.
- VaR ở đây là **lịch sử của chính danh mục** với tỷ trọng **đã thay đổi theo thời gian**. Bản P3
  (F3.R6) tính VaR với **tỷ trọng hiện tại** áp lên return lịch sử của từng tài sản (giá từ
  `snapshot_assets`). Cách đó phản ánh rủi ro hôm nay chính xác hơn.

### 4.5 Asset Allocation & Rebalancing Engine (`allocation.py`)

#### 4.5.1 Cấu hình

```yaml
allocation:
  enabled: true
  tolerance_pp: 5             # dải ±5 điểm % quanh mục tiêu (absolute)
  rebalance_to: target        # target | band_edge
  min_trade_usd: 20           # lệnh nhỏ hơn thì bỏ
  fee_rate: 0.001             # ước lượng phí taker Binance để hiển thị chi phí
  buckets:                    # target cộng lại phải = 100
    BTC:        { target: 50, symbols: [BTC, WBTC, BTCB] }
    ETH:        { target: 25, symbols: [ETH, WETH, STETH, WSTETH] }
    Stablecoin: { target: 15, symbols: "@stablecoins" }   # = settings.stablecoins
    Altcoins:   { target: 10, symbols: "*" }               # mọi symbol còn lại (đúng 1 bucket được dùng "*")
```

**Validate khi load:** tổng target = 100 (±0.01); mỗi symbol thuộc **đúng một** bucket; có tối đa một
bucket `"*"`; `tolerance_pp ∈ (0, 50)`. Sai thì endpoint trả `422` với thông báo cụ thể, dashboard vẫn
chạy bình thường.

#### 4.5.2 Thuật toán drift và lệnh gợi ý

Từ snapshot mới nhất, chỉ tính tài sản **có giá** (`price_usd > 0`):

$$V = \sum_b V_b, \qquad w_b = \frac{V_b}{V}, \qquad \text{drift}_b = w_b - w_b^{*}\ \ (\text{điểm \%})$$

$$\text{breach}_b = |\text{drift}_b| > \text{tolerance\_pp}$$

Nếu có bucket breach thì tính lệnh:

- `rebalance_to = target`: $\Delta_b = w_b^{*} V - V_b$ cho **mọi** bucket.
- `rebalance_to = band_edge` (ít turnover hơn): chỉ các bucket breach được kéo về mép dải gần nhất
  $w_b^{*} \pm \text{tol}$. Phần chênh được bù bằng các bucket trong dải, chia theo tỷ lệ khoảng trống còn
  lại của từng bucket tới mép dải đối diện.

Ràng buộc: $\sum_b \Delta_b = 0$ (tự tài trợ, không cần nạp thêm), sai số làm tròn được dồn vào bucket
Stablecoin. Bỏ lệnh có $|\Delta_b| <$ `min_trade_usd`. Action = `BUY` nếu $\Delta_b > 0$, `SELL` nếu
$< 0$. Số lượng ước tính = $\Delta_b / P$ cho tài sản lớn nhất của bucket.

**Ràng buộc thanh khoản:** vị thế **Simple Earn locked** không bán được trước hạn. Nếu một bucket phải
`SELL` mà phần liquid (spot + flexible + funding + on-chain) không đủ thì giảm lệnh xuống mức liquid và
ghi `"constrained_by": "locked_earn"`. Sau P0.3, `detail = locked` cho biết vị thế nào bị khoá.

> 🔒 Engine **chỉ gợi ý**, không bao giờ đặt lệnh. Điều này nhất quán với nguyên tắc read-only theo cấu
> trúc (key không có quyền trade).

```jsonc
// số liệu minh hoạ
GET /api/analytics/allocation
200 {
  "as_of": "…", "total_priced_usd": 50770.12, "unpriced_assets": 28, "tolerance_pp": 5,
  "buckets": [
    {"name": "BTC", "target_pct": 50, "actual_pct": 41.2, "drift_pp": -8.8, "breach": true,  "value_usd": 20917.3},
    {"name": "Stablecoin", "target_pct": 15, "actual_pct": 33.0, "drift_pp": 18.0, "breach": true, "value_usd": 16754.1}
  ],
  "actions": [
    {"bucket": "BTC", "action": "BUY",  "usd": 4467.8, "est_qty": 0.0451, "asset": "BTC"},
    {"bucket": "Stablecoin", "action": "SELL", "usd": 9138.6, "asset": "USDC", "constrained_by": null}
  ],
  "est_fees_usd": 13.6, "rebalance_needed": true
}
```

UI: thanh ngang xếp chồng "Thực tế" và "Mục tiêu" cho từng bucket, kèm vạch dải ±tol. Bucket breach được
viền màu cảnh báo. Danh sách action có nút "copy" (chỉ copy text).

### 4.6 Concentration Risk (`concentration.py`)

Trên snapshot mới nhất, tài sản có giá, $w_i = V_i / V$ theo `symbol` (gộp mọi nguồn):

| Chỉ số | Công thức | Diễn giải |
|---|---|---|
| **HHI** | $\text{HHI} = \sum_i w_i^2 \in [1/N, 1]$ | Trả cả thang 0–10 000 ($\times 10^4$) quen thuộc |
| **HHI chuẩn hoá** | $\text{HHI}^{*} = \dfrac{\text{HHI} - 1/N}{1 - 1/N}$ | So sánh được giữa các N khác nhau |
| **Số tài sản hiệu dụng** | $N_{\text{eff}} = 1/\text{HHI}$ | "Danh mục đa dạng tương đương N tài sản chia đều" |
| **CR3** | tổng 3 $w_i$ lớn nhất | Top-3 concentration ratio |
| **CEX vs Self-custody** | $\sum$ `binance_*` / $V$ và `onchain` / $V$ | Rủi ro đối tác (counterparty) |

- Hai biến thể: **raw** (mỗi stablecoin là một tài sản) và **economic** (gộp mọi stablecoin thành một
  "USD"). Biến thể economic phản ánh rủi ro thật tốt hơn vì USDT và USDC không đa dạng hoá cho nhau về
  mặt giá.
- Ngưỡng cảnh báo (cấu hình được): HHI > 2500 (tập trung cao theo chuẩn DOJ), CR3 > 80%,
  CEX > 50% ("tài sản trên sàn vượt 50%"). Hiện CEX ≈ 93%, nên cảnh báo này sẽ bật ngay từ ngày đầu.
  Đó là thông tin giá trị nhất mà module này mang lại.
- Bonus: HHI theo **sàn/ví** (Binance, từng ví on-chain) và theo **chain**.

### 4.7 Checklist F3

**Nền chuỗi dữ liệu**

- [ ] **F3.S1** `series.py`: NAV close ngày, gap, Modified Dietz $r_d$, chỉ số $I$ — **P1** (cần P0.2; F2.6 cho bản khử flow)
  - AC1: không flow thì $r_d = V_d/V_{d-1} - 1$ chính xác tới 1e-12.
  - AC2: nạp $1000 giữa ngày ($w = 0.5$), thị trường đứng yên → $r_d = 0$.
  - AC3: ngày gap → một quan sát với `gap_days = 2`; snapshot có nguồn lỗi không được chọn làm close.
  - AC4: khi F2 tắt, payload có `flow_adjusted: false`.

**TWR / MWR / PnL**

- [ ] **F3.T1** TWR theo kỳ + năm hoá chỉ khi ≥ 365 ngày — **P1**
  - AC: chuỗi $r = [+10\%, -10\%]$ → TWR = −1.00%; kỳ 30 ngày không có trường `annualized`.
- [ ] **F3.T2** MWR (XIRR) Newton + bisection fallback — **P2**
  - AC1: không có flow → MWR kỳ = TWR kỳ (sai số < 1e-8).
  - AC2: case kinh điển: đầu tư 100, sau 1 năm nạp thêm 100, cuối năm 2 giá trị 230 → so với nghiệm tính tay (giải bậc hai) sai số < 1e-6.
  - AC3: không có đổi dấu → `null` + `reason`, không raise.
- [ ] **F3.P1** Bảng PnL 7d/30d/90d/YTD/ALL (§4.2.3) + `GET /api/analytics/pnl` — **P1**
  - AC: `pnl_usd = nav_change − net_flows` trong mọi kỳ; `coverage_days` đúng khi lịch sử ngắn hơn kỳ; YTD dùng `day_boundary_tz`.
- [ ] **F3.P2** UI: hàng thẻ PnL dưới hero (chạm để đổi USD ↔ %) — **P2**

**Benchmark**

- [ ] **F3.B1** Migration v5 `benchmark_prices` + sync BTC/ETH qua Binance klines trong collector — **P2**
  - AC: backfill 365 nến ngày; chạy lại là no-op; Binance lỗi không làm hỏng `record`.
- [ ] **F3.B2** Sync SPX (Yahoo → Stooq fallback) — **P2**
  - AC: cả hai nguồn lỗi thì SPX bị đánh dấu unavailable, BTC/ETH không bị ảnh hưởng.
- [ ] **F3.B3** Base 100 + căn lịch (§4.3.3) — **P2**
  - AC: base 100 của danh mục dùng $I$ (TWR), không dùng NAV; test SPX gộp return cuối tuần.
- [ ] **F3.B4** Beta, ρ, TE, α, IR (§4.3.4) + `GET /api/analytics/benchmark` — **P2**
  - AC1: $r^p = 2 r^b$ → β = 2, ρ = 1. $r^p = r^b$ → TE = 0, α ≈ 0.
  - AC2: so với `numpy.cov`/`corrcoef` trên dữ liệu ngẫu nhiên, sai số < 1e-10.
  - AC3: `n < 30` → các chỉ số là `null`, chart base 100 vẫn có.
- [ ] **F3.B5** UI chart so sánh (nhiều line, Lightweight Charts) + bảng chỉ số — **P2**

**Risk & Volatility**

- [ ] **F3.R1** MDD, HWM, ngày đỉnh/đáy/hồi phục, chuỗi underwater — **P2**
  - AC: chỉ số $[100, 120, 90, 130]$ → MDD = −25%, đỉnh = ngày 2, đáy = ngày 3, hồi phục = ngày 4. Rút 50% NAV **không** tạo drawdown.
- [ ] **F3.R2** σ ngày & năm hoá (+ rolling 30d) — **P2**
  - AC: khớp `np.std(r, ddof=1) * sqrt(365)`; loại quan sát gap/unstable.
- [ ] **F3.R3** Sharpe — **P2**
  - AC: kiểm tra với chuỗi tổng hợp có mean/std đã biết; $R_f$ đọc từ config.
- [ ] **F3.R4** Sortino (downside trên toàn bộ $n$) — **P2**
  - AC: test phân biệt rõ với cách sai (std của return âm); không có ngày âm → `null`, không `inf`.
- [ ] **F3.R5** Historical VaR 95% + CVaR (`method="lower"`) — **P2**
  - AC: 100 return $\{-0.10, -0.09, …\}$ đã biết → VaR = đúng quan sát thứ 5 nhỏ nhất × $V_T$; `n < 60` → `null`.
- [ ] **F3.R6** VaR theo tỷ trọng hiện tại (return lịch sử từng tài sản × trọng số hôm nay) — **P3**
- [ ] **F3.R7** `GET /api/analytics/risk` + UI: thẻ rủi ro + underwater chart (`BaselineSeries`) — **P2**
  - AC: mỗi thẻ hiện `n_obs`; `sufficient=false` → thẻ xám "cần thêm N ngày dữ liệu".

**Allocation & Rebalancing**

- [ ] **F3.A1** Parse + validate block `allocation` trong `config.py` — **P2**
  - AC: tổng ≠ 100, symbol trùng bucket, hai bucket `"*"` → mỗi lỗi một thông báo rõ.
- [ ] **F3.A2** Drift + lệnh `target` / `band_edge` + `min_trade_usd` + Σ = 0 — **P2**
  - AC1: không bucket nào breach → `actions = []`.
  - AC2: Σ `usd` có dấu của actions = 0 (±$0.01).
  - AC3: `band_edge` có turnover ≤ `target` trên 100 portfolio ngẫu nhiên.
- [ ] **F3.A3** Ràng buộc locked Earn (cần P0.3) — **P3**
  - AC: bucket cần SELL $5000 mà chỉ có $3000 liquid → lệnh $3000 + `constrained_by`.
- [ ] **F3.A4** `GET /api/analytics/allocation` + UI thanh mục tiêu/thực tế — **P2**

**Concentration**

- [ ] **F3.C1** HHI, HHI*, N_eff, CR3 (raw + economic) — **P1** (chỉ cần snapshot hiện tại, giá trị có ngay)
  - AC: 4 tài sản chia đều → HHI = 0.25, HHI* = 0, N_eff = 4; 1 tài sản → HHI = 1; gộp USDT+USDC làm HHI economic ≥ raw.
- [ ] **F3.C2** CEX vs self-custody + HHI theo sàn/ví/chain — **P1**
  - AC: tỷ lệ CEX khớp `groups.binance / groups.all` của `/api/portfolio`.
- [ ] **F3.C3** `GET /api/analytics/concentration` + thẻ cảnh báo trên UI — **P1**
  - AC: CEX > 50% → cảnh báo hiện (sẽ hiện ngay với dữ liệu hôm nay).

**Tích hợp**

- [ ] **F3.U1** Màn "Insights" trong PWA (tab mới cạnh danh sách tài sản), gom PnL / Risk / Benchmark / Allocation / Concentration — **P2**
  - AC: màn 360px không tràn ngang; mọi số USD có `.priv`; mỗi thẻ có trạng thái thiếu dữ liệu riêng.
- [ ] **F3.U2** `main.py report --period 30d [--json]`: báo cáo text tổng hợp (dùng cho cron gửi mail tuần) — **P3**

---

## 5. Master Ledger Summary

> Trạng thái tính đến **2026-11-25**. Cập nhật bảng này cùng lúc với checklist trong Epic (quy tắc §0.2).

| ID | Task | Epic | Ưu tiên | Trạng thái | File đích | Phụ thuộc | Hoàn thành (ngày · commit) |
|---|---|---|---|---|---|---|---|
| P0.1 | Migration `user_version` + backup | P0 | P0 | `[x] DONE` | `services/db.py` | — | | 2026-11-25 |
| P0.2 | `snapshot_sources` + `snapshots.trigger` + backfill hố | P0 | P0 | `[x] DONE` | `services/db.py`, `services/portfolio.py` | P0.1 | | 2026-11-25 |
| P0.3 | Lưu `detail`, `price_id` | P0 | P0 | `[x] DONE` | `services/db.py`, `services/portfolio.py` | P0.1 | | 2026-11-25 |
| P0.4 | WAL mode | P0 | P0 | `[x] DONE` | `services/db.py` | — | | 2026-11-25 |
| P0.5 | Cron 1h/lần | P0 | P0 | `[x] DONE` | crontab | — | 2026-11-25 |
| P0.6 | Web token + bind LAN + unit systemd | P0 | P0 | `[ ] TODO` | env, `deploy/crypto-web.service` | — | |
| P0.7 | Multicall3 ERC-20 | P0 | P0 | `[ ] TODO` | `clients/onchain.py` | — | |
| P0.8 | Backfill 5 năm (60 snapshots) | P0 | P0 | `[ ] TODO` | `services/backfill.py` | — | |
| F1.1 | Loader state `(symbol, source)` | F1 | P1 | `[ ] TODO` | `services/analytics/attribution.py` | — | |
| F1.2 | Engine phân rã thuần + bất biến | F1 | P1 | `[ ] TODO` | `services/analytics/attribution.py` | F1.1 | |
| F1.3 | `chained` / `endpoint` | F1 | P1 | `[ ] TODO` | `services/analytics/attribution.py` | F1.2 | |
| F1.4 | Bucket `revaluation`, `data_gap` | F1 | P1 | `[ ] TODO` | `services/analytics/attribution.py` | F1.2, P0.2 | |
| F1.5 | `GET /api/portfolio/attribution` | F1 | P1 | `[ ] TODO` | `web_api.py` | F1.3 | |
| F1.6 | Câu `summary` tiếng Việt | F1 | P2 | `[ ] TODO` | `services/analytics/attribution.py` | F1.5 | |
| F1.7 | UI sheet "Vì sao biến động?" | F1 | P1 | `[ ] TODO` | `static/app.js`, `static/index.html`, `static/style.css` | F1.5 | |
| F1.8 | v2: flow/transfer/income/trade | F1 | P2 | `[ ] TODO` | `services/analytics/attribution.py` | F2.6, F2.8 | |
| F1.9 | CLI `main.py why` | F1 | P3 | `[ ] TODO` | `main.py` | F1.5 | |
| F2.1 | Migration v4 `cash_flows`, `sync_state` | F2 | P1 | `[x] DONE` | `services/db.py` | P0.1 | | 2026-11-25 |
| F2.2 | Binance deposit/withdraw history | F2 | P1 | `[ ] TODO` | `clients/binance.py` | — | |
| F2.3 | `sync()` + cursor + nối vào `record` | F2 | P1 | `[ ] TODO` | `services/cash_flows.py`, `main.py` | F2.1, F2.2 | |
| F2.4 | Chuẩn hoá network/địa chỉ/thời gian | F2 | P1 | `[ ] TODO` | `services/cash_flows.py` | — | |
| F2.5 | Delta on-chain detector | F2 | P1 | `[ ] TODO` | `services/cash_flows.py` | P0.3, F2.1 | |
| F2.6 | Matcher tầng 1+3, một-một, dòng phí | F2 | P1 | `[ ] TODO` | `services/cash_flows.py` | F2.3, F2.4, F2.5 | |
| F2.7 | In-transit + chốt | F2 | P1 | `[ ] TODO` | `services/cash_flows.py` | F2.6 | |
| F2.8 | Income: Earn rewards, dividend | F2 | P2 | `[ ] TODO` | `clients/binance.py`, `services/cash_flows.py` | F2.3 | |
| F2.9 | TxVerifier (tầng 2) + swap (tầng 4) | F2 | P2 | `[ ] TODO` | `services/cash_flows.py`, `clients/onchain.py` | F2.6 | |
| F2.10 | `GET`/`PATCH /api/cash-flows` | F2 | P2 | `[ ] TODO` | `web_api.py` | F2.6 | |
| F2.11 | UI tab "Dòng tiền" | F2 | P2 | `[ ] TODO` | `static/*` | F2.10, F1.7 | |
| F2.12 | `change_24h.flow_adjusted_usd` | F2 | P2 | `[ ] TODO` | `web_api.py`, `static/app.js` | F2.6 | |
| F2.13 | Settings Test: quyền đọc history | F2 | P3 | `[ ] TODO` | `web_api.py`, `static/app.js` | F2.2 | |
| F2.14 | Explorer API (lịch sử tx đầy đủ) | F2 | P3 | `[ ] TODO` | `clients/onchain.py` | F2.9 | |
| F3.S1 | Chuỗi ngày + Modified Dietz + chỉ số $I$ | F3 | P1 | `[ ] TODO` | `services/analytics/series.py` | P0.2 (F2.6 để khử flow) | |
| F3.T1 | TWR | F3 | P1 | `[ ] TODO` | `services/analytics/returns.py` | F3.S1 | |
| F3.T2 | MWR / XIRR | F3 | P2 | `[ ] TODO` | `services/analytics/returns.py` | F3.S1, F2.6 | |
| F3.P1 | PnL 7d/30d/90d/YTD/ALL + endpoint | F3 | P1 | `[ ] TODO` | `services/analytics/returns.py`, `web_api.py` | F3.T1 | |
| F3.P2 | UI thẻ PnL | F3 | P2 | `[ ] TODO` | `static/*` | F3.P1 | |
| F3.B1 | `benchmark_prices` + BTC/ETH klines | F3 | P2 | `[x] DONE` | `services/analytics/benchmark.py`, `services/db.py` | P0.1 | | 2026-11-25 |
| F3.B2 | SPX Yahoo/Stooq | F3 | P2 | `[ ] TODO` | `services/analytics/benchmark.py` | F3.B1 | |
| F3.B3 | Base 100 + căn lịch | F3 | P2 | `[ ] TODO` | `services/analytics/benchmark.py` | F3.S1, F3.B1 | |
| F3.B4 | β, ρ, TE, α, IR + endpoint | F3 | P2 | `[ ] TODO` | `services/analytics/risk.py`, `web_api.py` | F3.B3 | |
| F3.B5 | UI chart so sánh benchmark | F3 | P2 | `[ ] TODO` | `static/*` | F3.B4 | |
| F3.R1 | MDD + underwater | F3 | P2 | `[ ] TODO` | `services/analytics/risk.py` | F3.S1 | |
| F3.R2 | Volatility năm hoá | F3 | P2 | `[ ] TODO` | `services/analytics/risk.py` | F3.S1 | |
| F3.R3 | Sharpe | F3 | P2 | `[ ] TODO` | `services/analytics/risk.py` | F3.R2 | |
| F3.R4 | Sortino | F3 | P2 | `[ ] TODO` | `services/analytics/risk.py` | F3.R2 | |
| F3.R5 | Historical VaR 95% + CVaR | F3 | P2 | `[ ] TODO` | `services/analytics/risk.py` | F3.S1 | |
| F3.R6 | VaR theo tỷ trọng hiện tại | F3 | P3 | `[ ] TODO` | `services/analytics/risk.py` | F3.R5 | |
| F3.R7 | Endpoint risk + UI underwater | F3 | P2 | `[ ] TODO` | `web_api.py`, `static/*` | F3.R1–R5 | |
| F3.A1 | Config `allocation` + validate | F3 | P2 | `[ ] TODO` | `config.py` | — | |
| F3.A2 | Drift + lệnh gợi ý | F3 | P2 | `[ ] TODO` | `services/analytics/allocation.py` | F3.A1 | |
| F3.A3 | Ràng buộc locked Earn | F3 | P3 | `[ ] TODO` | `services/analytics/allocation.py` | F3.A2, P0.3 | |
| F3.A4 | Endpoint + UI allocation | F3 | P2 | `[ ] TODO` | `web_api.py`, `static/*` | F3.A2 | |
| F3.C1 | HHI, HHI*, N_eff, CR3 | F3 | P1 | `[ ] TODO` | `services/analytics/concentration.py` | — | |
| F3.C2 | CEX vs self-custody, HHI theo sàn/chain | F3 | P1 | `[ ] TODO` | `services/analytics/concentration.py` | F3.C1 | |
| F3.C3 | Endpoint + thẻ cảnh báo concentration | F3 | P1 | `[ ] TODO` | `web_api.py`, `static/*` | F3.C2 | |
| F3.U1 | Màn "Insights" | F3 | P2 | `[ ] TODO` | `static/*` | F3.P1, F3.C3 | |
| F3.U2 | CLI `main.py report` | F3 | P3 | `[ ] TODO` | `main.py` | F3.P1, F3.R7 | |

**Tổng hợp:** 7 task P0 · 19 task P1 · 24 task P2 · 6 task P3 · **56 task**, 0 DONE.

---

## 6. Lộ trình milestone & đồ thị phụ thuộc

### 6.1 Milestone

| Milestone | Nội dung | Điều kiện hoàn thành | Ghi chú |
|---|---|---|---|
| **M0: Dữ liệu sạch** | P0.1–P0.7 | Lịch sử có trạng thái nguồn, có `detail`, nhịp 1h, dashboard có token | Làm P0.5 ngay để bắt đầu tích luỹ dữ liệu |
| **M1: "Vì sao?" + rủi ro tập trung** | F1.1–F1.5, F1.7, F3.C1–C3 | Chạm chip 24h ra được phân rã; thẻ concentration báo CEX 93% | Giá trị dùng được ngay với 3 ngày dữ liệu |
| **M2: Dòng tiền** | F2.1–F2.7, F3.S1, F3.T1, F3.P1 | Rút Binance → Ledger không hiện thành lỗ; PnL thật 7d/30d | |
| **M3: Định lượng** | F2.8–F2.12, F1.6, F1.8, F3.B*, F3.R1–R5, R7, F3.A1–A2, A4, F3.U1, F3.T2, F3.P2 | Màn Insights đầy đủ khi đã có ≥ 30 ngày dữ liệu | Thời điểm sớm nhất có ý nghĩa: khoảng cuối tháng 12/2026 nếu P0.5 xong trong tuần này |
| **M4: Hoàn thiện** | Mọi task P3 | — | |

### 6.2 Đồ thị phụ thuộc (rút gọn)

```
P0.1 ─┬─► P0.2 ─┬──────────────► F1.4
      │         └──► F3.S1 ──┬─► F3.T1 ─► F3.P1 ─► F3.U1
      │                      ├─► F3.R1..R5 ─► F3.R7
      │                      └─► F3.B3 ─► F3.B4 ─► F3.B5
      ├─► P0.3 ──► F2.5 ─┐
      ├─► F2.1 ─► F2.3 ──┼─► F2.6 ─┬─► F2.7
      │   F2.2 ──┘       │         ├─► F1.8 ◄── F2.8
      │   F2.4 ──────────┘         ├─► F2.10 ─► F2.11
      │                            ├─► F3.T2
      │                            └─► (F3.S1 khử flow)
      └─► F3.B1 ─► F3.B2
F1.1 ─► F1.2 ─► F1.3 ─► F1.5 ─► F1.7
F3.C1 ─► F3.C2 ─► F3.C3                     (không phụ thuộc gì, làm song song từ ngày đầu)
F3.A1 ─► F3.A2 ─► F3.A4
P0.7 ─► P0.5
```

---

## 7. Nhật ký quyết định

| ID | Quyết định | Phương án bị loại | Lý do |
|---|---|---|---|
| D1 | Attribution tính ở grain `(symbol, source)`, hiển thị ở `symbol` | Tính thẳng ở `symbol` | Giá trung bình theo giá trị đổi khi chuyển giữa nguồn, sinh price effect giả (§2.2.2) |
| D2 | Chỉ numpy + stdlib cho analytics | pandas, scipy | Giữ dependency nhẹ; dữ liệu nhỏ (≤ vài nghìn điểm ngày); XIRR/quantile tự viết được và test được |
| D3 | Benchmark BTC/ETH lấy từ Binance klines | CoinGecko | Không cần key, cùng nguồn giá với danh mục, host đã chạy được trên máy này |
| D4 | Ranh giới ngày mặc định UTC | Europe/Paris | Khớp nến ngày crypto 00:00 UTC; cấu hình lại được qua `analytics.day_boundary_tz` |
| D5 | MDD/benchmark dùng chỉ số TWR $I$, không dùng NAV | NAV thô | Rút tiền không phải drawdown |
| D6 | Return ngày bằng Modified Dietz với timestamp flow thật | Giả định flow ở cuối ngày | Binance cho timestamp chính xác tới ms; chi phí như nhau |
| D7 | Matching 4 tầng, tầng xác định (address/tx) trước heuristic | Chỉ heuristic số lượng + thời gian | Rút tới địa chỉ ví đã cấu hình là bằng chứng chắc chắn, không cần đoán |
| D8 | Mọi dữ liệu ngoài được collector đồng bộ vào SQLite; `GET` không gọi mạng | Gọi API lúc người dùng mở màn | Giữ nguyên nguyên tắc hiện có, mở app nhanh, không lộ rate limit ra UI |
| D9 | Gateway public không mở thêm route | Thêm `/v1/…/pnl` | Bất biến T3: lộ key gateway chỉ lộ đúng một con số |
| D10 | Không annualize return của kỳ < 1 năm | Luôn annualize | Theo GIPS; con số năm hoá từ 2 tuần dữ liệu gây hiểu nhầm |

---

## 8. Changelog của Ledger

| Ngày | Thay đổi | Người |
|---|---|---|
| 2026-11-25 | Tạo tài liệu: P0 (7 task), F1 (9), F2 (14), F3 (26). Tổng 56 task, tất cả TODO. | Claude (Opus 5.5) cho Jean |
