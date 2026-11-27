# ROADMAP — `crypto_portfolio`

> Việc **sắp làm**, theo thứ tự ưu tiên. Cập nhật 2026-11-27.
>
> - Trạng thái từng task, quyết định, changelog: [`docs/LEDGER.md`](docs/LEDGER.md)
> - Công thức, schema, Acceptance Criteria chi tiết của Phase 1–2: [`docs/SPEC.md`](docs/SPEC.md)
> - Hệ thống đang chạy thế nào: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

## 1. Đang ở đâu

| Giai đoạn | Nội dung | Tình trạng |
|---|---|---|
| **Phase 1** (P0, F1–F3) | Dữ liệu sạch nhịp 1h, backfill 5 năm, attribution, dòng tiền Binance, TWR / Base 100 / drawdown / volatility | Phần lõi xong. Còn lại ở §4. |
| **Phase 2, W1–W3** (M4, M6, M7) | Tab Activity, tài sản manual (ví lạnh, staking), trợ lý Allocation | Xong. |
| **Phase 3, W4–W6** (N1–N9) | Chín ý rút ra sau khi vận hành thật | W4 xong code/test (2026-11-27, 619 test, schema v7): A1, N2, N3 PASS; **N1.2 PARTIAL** (APR thực nhận 7d chưa đủ dữ liệu), xem "Kết quả kiểm" cuối mục W4. W5 là việc kế tiếp. |
| **W7, W8** (M5, M8) | Telegram, widget mobile | Hoãn (D16), §3. |

Nguyên tắc không đổi cho mọi việc bên dưới: app chỉ **mô tả** hiện trạng, không phát tín hiệu
mua/bán, không đặt lệnh; key Binance chỉ đọc; `GET` không gọi mạng; gateway public không thêm route.

## 2. Việc kế tiếp: Phase 3 (N1–N9)

Chín ý, gộp từ 7 đề xuất của Fable 5.2 và 6 gợi ý của Jean/assistant, xếp theo giá trị trên công sức.
Effort: **S** ≈ 1 ngày · **M** ≈ vài ngày · **L** ≈ một tuần trở lên. Khi mở một ý, tách task con
`N<k>.<n>` kèm AC ngay trong file này.

| Hạng | ID | Ý tưởng | Effort | Wave | Vì sao ở hạng này |
|---|---|---|---|---|---|
| 1 | N1 | Earn Yield Lens | S | W4 | ≈ 93% NAV nằm ở Earn, dữ liệu đã có: chỉ cần tính rồi hiển thị |
| 2 | N2 | Đối soát số lượng + sức khoẻ dữ liệu | S/M | W4 | Đang sai thật: 25–27/11 app ghi 0 trade dù số lượng đổi 38–71% |
| 3 | N3 | Peg check | S | W4 | Rẻ nhất, che một rủi ro đang vô hình |
| 4 | N4 | Trade ledger & giá vốn | M/L | W5 | Giá trị cao nhất, tốn công nhất; chữa tận gốc cho N2 |
| 5 | N5 | Analyst kit | S | W5 | Rẻ, nhưng nên chốt sau khi bảng `trades` của N4 ổn định |
| 6 | N6 | Kế hoạch giải ngân + stress | M | W6 | Hữu ích lúc đang giải ngân stablecoin; cần config và thẻ UI mới |
| 7 | N7 | EUR lens | M | W6 | Tách được hiệu ứng tỷ giá, nhưng đụng nhiều chỗ UI |
| 8 | N8 | NAV vs vốn đã nạp | S/M | W6 | Tách lãi thị trường khỏi tiền mới; trùng một phần với PnL đã có |
| 9 | N9 | Nhật ký đầu tư | S/M | W6 | Giá trị tích luỹ dần, không gấp |

Phụ thuộc: `N2 → N4 → N5` (trade thật thay trade suy luận; view chốt sau `trades`) ·
`N4 → F1.8` (bucket `trade` của attribution) · `N3 → N7` (cùng cách lưu ticker tham chiếu) ·
N1, N6, N8, N9 độc lập.

### W4 — Quick win trên dữ liệu sẵn có

**Xong khi:** thấy APR thực nhận của từng tài sản Earn; biến động số lượng 25–27/11 được giải thích;
lệch peg > 0.3% có chip cảnh báo.

**A1 · Analytics gập được** (S) — làm trước N1.4
- **Vấn đề:** tab Analytics là một cột thẻ dài; mỗi ý mới làm trang dài thêm.
- **Cách làm:** mỗi thẻ gập lại còn tiêu đề + một dòng tóm tắt (con số trả lời câu hỏi của thẻ),
  bấm để mở nội dung hiện có. Mở nhiều thẻ cùng lúc được; trạng thái mở/gập nhớ trên thiết bị.
  Chia hai nhóm: "Over the period" (Why it moved, Performance, Risk: theo bộ chọn kỳ) và
  "Right now" (Earn income, Allocation: không theo kỳ). Chỉ đụng `static/`. Xem `DESIGN.md`.
- **AC:** gập hết thì cả tab nằm trong một màn hình điện thoại; mỗi dòng gập hiện một con số tóm
  tắt (hoặc lý do chưa có); mở thẻ có chart thì chart vẽ đúng bề rộng; tải lại trang giữ nguyên
  thẻ đang mở; điều khiển bằng bàn phím được (`aria-expanded`).

**N1 · Earn Yield Lens** (S)
- **Vấn đề:** app không cho biết mỗi tài sản Earn thực nhận bao nhiêu %/năm, và APR niêm yết lệch
  APR thực nhận bao nhiêu.
- **Dữ liệu:** reward Earn đã đồng bộ trong `cash_flows` ÷ số dư Earn trong `snapshot_assets`; thêm
  trường APR mà API vị thế Flexible đã trả nhưng app chưa lưu. Đo thử 7 ngày: USDC ≈ 5.0%,
  SOL ≈ 2.1%, ETH ≈ 1.1%, BTC ≈ 0.03%.
- **Lưu ý:** cửa sổ ngắn thì nhiễu (bonus tier, số dư đổi giữa kỳ): ghi rõ cửa sổ, kèm
  `n_obs`/`sufficient`.
- **AC:** bảng theo tài sản hiện APR niêm yết, APR thực nhận 7d/30d, thu nhập USD/ngày; USDC 7d ra
  ≈ 5.0% (±0.2 pp) trên dữ liệu hiện tại.
- **Phạm vi đã chốt (2026-11-27):** APR thực nhận chỉ tính trên snapshot thật (từ 11/2026), không
  dựng lại số dư Earn quá khứ. Quá khứ chỉ trả lời "đã kiếm được bao nhiêu": tổng reward, không cần
  số dư. APR 7d/30d là lãi suất năm hoá, không phải return nên không vướng D10.

| Task | Nội dung | AC |
|---|---|---|
| N1.1 | Migration v6: cột `snapshot_assets.listed_apr` (NULL = không biết). `earn_flexible()` giữ trường APR của response thay vì bỏ. | Snapshot mới có `listed_apr` cho vị thế Flexible; snapshot cũ và nguồn khác là NULL; migrate DB v5 không mất dòng nào. |
| N1.2 | Hàm APR thực nhận: reward trong cửa sổ ÷ số dư Earn bình quân theo snapshot, năm hoá; kèm `n_obs`, `sufficient`. | USDC 7d ≈ 5.0% (±0.2 pp); cửa sổ có hố hoặc thiếu snapshot → `sufficient = false`, không ra số giả; số dư 0 → không chia. |
| N1.3 | Thu nhập luỹ kế: nâng `earn_backfill_days` lên mức xa nhất Binance cho; tổng reward theo tài sản và theo năm, quy USD bằng `value_usd` lúc nhận. | Có tổng theo năm cho từng tài sản và ngày bắt đầu thật của dữ liệu ("tính từ X"); tổng khớp `SUM(value_usd)` của reward Earn trong `cash_flows`. |
| N1.4 | `GET /api/analytics/earn` (chỉ đọc SQLite) + thẻ "Earn income" ở tab **Analytics**, nhóm "Right now", giữa Risk và Allocation (khuôn gập của A1): bảng APR niêm yết / thực nhận 7d, 30d / $ mỗi ngày, dòng "Received since X: $…" theo năm. Dòng tóm tắt khi gập: $/ngày và APR của tài sản Earn lớn nhất. | Số tiền mang `.priv`; chưa đủ dữ liệu → hiện "—" kèm lý do, không lỗi; `GET` không gọi mạng. |

**N2 · Đối soát số lượng + sức khoẻ dữ liệu** (S/M)
- **Vấn đề:** 25–27/11 số lượng đổi mạnh (BTC +46%, SOL +57%, PEPE +71%, LINK +38%, USDC −12%,
  USDT −100%; không có nạp, reward < 0.02%) nhưng app ghi **0 trade**. App cũng không báo khi
  snapshot cũ hay một nguồn lỗi.
- **Cách làm:** phần dư = Δ số lượng − reward − flow, theo symbol trên **tổng Binance**
  (Spot + Earn + Funding, để chuyển Spot ↔ Earn không thành trade giả). Chỉ Binance: ví on-chain
  chưa có lịch sử dòng tiền (F2.5), mọi lần chuyển vào ví sẽ bị đọc thành "chưa giải thích".
  Hai phần dư ngược dấu trong cùng giờ ⇒ "trade suy luận" theo giá snapshot. Badge sức khoẻ: tuổi snapshot, OK/lỗi
  theo nguồn (`snapshot_sources`), hố trên chart.
- **Lưu ý:** trade suy luận là ước lượng, phải gắn nhãn và nhường chỗ cho trade thật của N4. Badge
  chỉ thấy khi mở app nên không thay heartbeat của W7.
- **AC:** chạy lại 25–27/11 → các cặp stablecoin ↔ BTC/SOL/PEPE/LINK hiện thành trade suy luận, phần
  còn lại gom vào một dòng "chưa giải thích"; snapshot cũ quá 130 phút hoặc một nguồn lỗi → badge
  đổi trạng thái.
- **Phạm vi đã chốt (2026-11-27):** trade suy luận **tính lúc đọc**, không lưu bảng nào: khi N4 có
  trade thật thì xoá đoạn tính, không phải dọn dữ liệu. Một giờ mua nhiều coin → một dòng
  "USDC → BTC + SOL", không chia stablecoin cho từng coin.

| Task | Nội dung | AC |
|---|---|---|
| N2.1 | Phần dư giữa hai snapshot thật liên tiếp: Δ số lượng − reward − flow ngoài, gộp Binance theo symbol; bỏ qua cặp snapshot có nguồn lỗi và phần dư bụi (< $1). | Chuyển Spot → Earn cho phần dư 0; nạp/rút/reward đã có trong `cash_flows` cho phần dư 0; nguồn lỗi không sinh phần dư. |
| N2.2 | Ghép cặp: trong cùng khoảng snapshot, phần dư stablecoin và phần dư coin ngược dấu ⇒ một trade suy luận theo giá snapshot; phần không ghép được ⇒ "chưa giải thích". | Dữ liệu 25–27/11: stablecoin ↔ BTC/SOL/PEPE/LINK thành trade suy luận; phần còn lại nằm trong dòng "chưa giải thích". |
| N2.3 | Tab Activity: item `kind = 'trade'`, `inferred = true`, tag "inferred", có chip lọc; dòng "Unexplained" riêng (UI tiếng Anh). | Item hiện đúng giờ, hai vế và giá trị ước lượng; nhãn suy luận luôn hiển thị; `GET /api/cash-flows` không gọi mạng. |
| N2.4 | Sức khoẻ dữ liệu trên **dòng trạng thái sẵn có** dưới hero ("Live · updated …"), không thêm badge: tuổi snapshot chuyển màu cảnh báo khi quá 130 phút; số hố 24h gần nhất (`is_hole`) chỉ hiện khi > 0. Nguồn lỗi vẫn do notice "sources failed" sẵn có đảm nhận. | Snapshot cũ quá 130 phút → dòng tuổi đổi màu và có chữ "stale"; có hố trong 24h → hiện "N gaps"; mọi thứ ổn → dòng trạng thái y như hiện nay. |

**N3 · Peg check** (S) — thứ tự làm: N3 → A1 → N1 → N2
- **Vấn đề:** stablecoin luôn hiện $1.00 nên một lần lệch peg sẽ không ai thấy.
- **Dữ liệu:** ticker `USDCUSDT` đã tải ở mỗi snapshot (chưa lưu) + giá USD từ DefiLlama làm tham
  chiếu thứ hai.
- **Lưu ý:** `USDCUSDT` đo USDC so với USDT chứ không phải USD, nên bắt buộc có tham chiếu thứ hai.
  Vẫn giữ ghim $1.00 cho hiển thị và NAV.
- **AC:** mỗi snapshot lưu độ lệch; |lệch| > 0.3% → chip cảnh báo, dưới ngưỡng thì ẩn; tham chiếu
  lỗi → chip "không kiểm được", không chặn `record`.
- **Phạm vi đã chốt (2026-11-27):** chỉ kiểm stablecoin đang nắm giữ. USDC có hai tham chiếu
  (`USDCUSDT`, DefiLlama); USDT chỉ có DefiLlama.

| Task | Nội dung | AC |
|---|---|---|
| N3.1 | Migration v6 (chung với N1.1): bảng `peg_checks` (`snapshot_id`, `symbol`, `reference`, `price`, `error`). | Migrate DB v5 sạch; xoá snapshot thì dòng peg đi theo. |
| N3.2 | `record` ghi một dòng cho mỗi (stablecoin đang giữ, tham chiếu): `USDCUSDT` lấy từ ticker đã tải, giá USD từ DefiLlama. | Tham chiếu lỗi → dòng có `error`, `record` vẫn thành công và NAV vẫn ghim $1.00. |
| N3.3 | Pill trên dòng trạng thái dưới hero: độ lệch = tham chiếu lệch nhiều nhất còn dùng được của snapshot mới nhất. | \|lệch\| > 0.3% → chip "USDC −0.4%"; dưới ngưỡng → ẩn; mọi tham chiếu của một coin lỗi → chip "không kiểm được". |

**Kết quả kiểm trên máy chủ thật (W4, 2026-11-27):** code/test xong, `pytest -q` 619 pass, DB v7.
- A1, N2, N3: **PASS**. N2: hai vế lệch nhau quá 15% vẫn bị xếp vào "Unexplained"
  (`TRADE_TOLERANCE` trong `services/reconcile.py`).
- **N1.2: PARTIAL.** AC giữ nguyên (USDC 7d ≈ 5.0% ±0.2 pp, quyết định D20). Hôm nay realised 7d
  của USDC trả `None`, `sufficient = false`: snapshot mới phủ 5.42/7 ngày (cần ≥ 6.3, `MIN_COVERAGE`)
  và có hố 25.35 giờ (23/11 23:00:05Z → 25/11 00:21:21Z) > 6 giờ (`MAX_GAP_HOURS`); các hố > 6 giờ
  khác: 10.89, 7.26, 11.56, 10.92 giờ. Bỏ qua hai điều kiện thì ra **3.34%**
  (`earned / mean_balance × 365 / span_days` = 13.736688 USDC / 27,666.72 USDC bình quân trên 122
  snapshot / 5.4197 ngày; chỉ tính reward có `first < when <= last`).
  - Cửa sổ 7d đủ cả hai điều kiện sớm nhất: **~2026-12-01 07:33 UTC**. Dự kiến realised chỉ
    **~2.0–2.4%**: reward USDC giảm từ ~7.1–7.7 xuống ~1.5 USDC/ngày từ 23/11.
  - AC 5.0% **có nguy cơ FAIL**. Cần tìm nguyên nhân reward giảm: khuyến mãi kết thúc, hay rút
    ~3,500 USDC ngày 24–25/11 làm mất bonus.
- `listed_apr` (v7) = base `latestAnnualPercentageRate` + bonus `tierAnnualPercentageRate` tính
  theo phần số dư nằm trong từng band (`effective_apr` trong `clients/binance.py`); phần tách lưu ở
  `listed_base_apr`, `listed_tiers`. Tên hai trường đã kiểm trên response thật. 25,949 USDC ở
  2.02% + 3% trên 5,000 đầu niêm yết 2.60%.
- Giới hạn đã biết (chưa sửa code): band tier chồng nhau chưa có test; `earn_report` lấy
  `listed_tiers` từ dòng đầu tiên có giá trị của tài sản.
- `earn_backfill_days` mặc định 1827. Nếu `config.yaml` đang ghi 365 thì sửa tay; lần `record` kế
  tiếp tải lùi reward (vài phút, nối tiếp được nếu bị ngắt).

### W5 — Giá vốn & phân tích

**Xong khi:** có giá vào bình quân và PnL chưa thực hiện; `main.py export` ra CSV.

**N4 · Trade ledger & giá vốn** (M/L)
- **Vấn đề:** không có giá vốn nên không biết đang lãi/lỗ bao nhiêu trên từng coin, và từng lần mua
  có tốt hơn giữ USDC 5%/năm không.
- **Dữ liệu:** bảng mới `trades` (migration), nạp từ Binance Convert + lịch sử Spot qua
  `HistoryStream` sẵn có. Cho ra: giá vào bình quân, PnL chưa thực hiện, bảng điểm từng lần mua.
- **Lưu ý:** 33 lần nạp crypto từ ngoài không có giá vốn (phải chọn quy ước và gắn cờ); backfill
  Spot quét từng symbol nên chậm.
- **AC:** trade 25–27/11 hiện đúng số lượng và giá; với mọi symbol, mua − bán + nạp − rút + reward
  khớp số dư snapshot (trừ bụi); vị thế thiếu giá vốn được gắn cờ, không hiện PnL giả.

**N5 · Analyst kit** (S)
- **Vấn đề:** muốn phân tích tự do thì mỗi lần phải viết lại SQL trên schema thô.
- **Dữ liệu:** migration thêm view SQL gọn (vị thế theo ngày, return đã khử dòng tiền, dòng tiền
  ngoài, thu nhập Earn theo ngày) + `main.py export` ra CSV + notebook mẫu.
- **Lưu ý:** view trở thành hợp đồng schema; notebook dùng thư viện ngoài, app không thêm
  dependency (D2).
- **AC:** `main.py export` sinh một CSV cho mỗi view; notebook mẫu dựng lại đường NAV và return ngày
  khớp `GET /api/analytics/quant`; `requirements.txt` không đổi.

### W6 — Kế hoạch & góc nhìn

**Xong khi:** có thẻ kế hoạch giải ngân + stress, toggle EUR, đường vốn đã nạp, ghi chú theo ngày.
Không chặn việc nào; làm khi W4–W5 đã cho số liệu đáng tin.

**N6 · Kế hoạch giải ngân + stress** (M)
- **Vấn đề:** kế hoạch giải ngân nằm ngoài app: không thấy tiến độ, cũng không thấy rủi ro tăng thế
  nào khi đi hết kế hoạch.
- **Dữ liệu:** block config mới (bucket nguồn, tổng, mỗi đợt, nhịp) + số dư và tỷ trọng từ snapshot.
  Cho ra: tiến độ từng đợt, câu lệnh đợt kế tiếp, dòng "BTC −40% làm danh mục mất X% hôm nay, Y% khi
  đạt mục tiêu", thanh trượt cú sốc BTC (mặc định −10%).
- **Lưu ý:** phải chốt giả định altcoin phản ứng thế nào khi BTC giảm trước khi làm. HHI/top-3 thuộc
  F3.C1–C3 (chưa làm, §4), không làm lại ở đây.
- **AC:** với config mẫu, thẻ hiện "đợt k/n, còn $Z", câu lệnh kế tiếp copy được, hai con số stress
  hôm nay / tại mục tiêu; chưa cấu hình → thẻ hướng dẫn, không lỗi.

**N7 · EUR lens** (M)
- **Vấn đề:** mọi con số là USD, không tách được phần biến động do tỷ giá EUR/USD.
- **Dữ liệu:** `EURUSDT` từ ticker đã tải (lưu thêm mỗi snapshot); các tháng backfill cần klines EUR.
- **Lưu ý:** `EURUSDT` là EUR so với USDT, không phải USD (cùng giới hạn như N3); đoạn lịch sử thiếu
  tỷ giá thì ẩn EUR, không nội suy.
- **AC:** toggle USD/EUR đổi hero và chart, vẫn giữ `.priv`; attribution có bucket "hiệu ứng tỷ giá"
  và tổng các bucket vẫn khớp Δ NAV tính bằng EUR.

**N8 · NAV vs vốn đã nạp** (S/M)
- **Vấn đề:** đường NAV đi lên không cho biết phần nào là lãi thị trường, phần nào là tiền mới nạp.
- **Dữ liệu:** dòng tiền ngoài luỹ kế từ `cash_flows` (đã loại chuyển nội bộ) + NAV từ `snapshots`.
  `GET /api/portfolio/pnl` đã có số liệu 7d/30d nhưng chưa có UI.
- **Lưu ý:** crypto nạp từ ngoài tính theo giá lúc nạp, không phải giá mua thật; đoạn lịch sử chưa
  có dữ liệu dòng tiền phải đánh dấu, không vẽ như thể vốn bằng 0.
- **AC:** tab Portfolio có đường "vốn đã nạp ròng" cạnh NAV; mức thay đổi khoảng cách hai đường
  trong 30 ngày khớp PnL 30d của endpoint; nạp $5,000 khi giá đứng yên → hai đường cùng nhảy, khoảng
  cách không đổi.

**N9 · Nhật ký đầu tư** (S/M)
- **Vấn đề:** lý do của từng quyết định không nằm cạnh con số, vài tháng sau không đọc lại được.
- **Dữ liệu:** bảng mới `notes` (ngày hoặc `snapshot_id`, nội dung): dữ liệu do người dùng nhập đầu
  tiên trong DB.
- **Lưu ý:** cần endpoint ghi mới (sau token); ghi chú không tái tạo được nên phải nằm trong backup;
  nội dung là văn bản thuần, escape khi hiển thị.
- **AC:** thêm/sửa/xoá ghi chú cho một ngày; thẻ Attribution của kỳ chứa ngày đó hiện ghi chú; ghi
  chú còn nguyên sau migrate và backup/restore.

## 3. Đang hoãn (D16)

Giữ nguyên spec và AC trong [`docs/SPEC.md`](docs/SPEC.md) §9.2, §9.5; không làm cho tới khi Jean mở lại.

| Wave | Task | Nội dung | Ghi chú khi mở lại |
|---|---|---|---|
| **W7** | M5.1–M5.5 | Telegram: digest 08:00, cảnh báo biến động > 5%, heartbeat cron | Nên làm sớm: dữ liệu mất do cron dừng là mất vĩnh viễn, và badge của N2 chỉ thấy khi mở app |
| **W8** | M8.1–M8.3 | Gateway `/summary` + widget iOS | Đổi bất biến bảo mật của gateway (D11): làm sau cùng |

Bị chặn theo: M5.6, M5.7, M7.6 (cần W7) · M8.4, M8.5 (cần W8).

## 4. Việc còn mở của Phase 1–2, chưa xếp lịch

Không chặn Phase 3. Lấy ra làm khi có lý do cụ thể; chi tiết và AC ở SPEC, trạng thái ở LEDGER.

| Nhóm | Task | Ghi chú |
|---|---|---|
| Dòng tiền on-chain | F2.5, F2.9, F2.14 | Hiện chỉ Binance và tài sản manual sinh flow. Chuyển ví ↔ ví on-chain chưa được nhận diện. |
| Attribution v2 | F1.6, F1.8, F1.9 | F1.8 nên chờ N4 (trade thật). |
| Rủi ro & hiệu suất | F3.T2, F3.R3–R6, F3.B2, F3.B4, F3.C1–C3 | MWR/XIRR, Sharpe, Sortino, VaR, SPX, β, concentration. Cần ≥ 30 ngày dữ liệu ngày mới có nghĩa. |
| Hiển thị PnL | F3.P2, F2.12 | Endpoint PnL đã có, chưa có thẻ UI. N8 phủ phần lớn nhu cầu này. |
| Hiệu năng RPC | P0.7 | Multicall3. Chỉ cần khi RPC public bắt đầu giới hạn tốc độ. |
| Lặt vặt (P3) | F2.13, F3.U2, M4.7, M6.5, M7.7 | Test quyền đọc lịch sử, CLI report, badge tab, watch-only BTC, sửa mục tiêu trong Settings. |
