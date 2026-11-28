# ROADMAP — `crypto_portfolio`

> Việc **sắp làm**. Cập nhật 2026-11-28.
>
> - Trạng thái từng task, quyết định, changelog: [`docs/LEDGER.md`](docs/LEDGER.md)
> - Công thức, schema, Acceptance Criteria: [`docs/SPEC.md`](docs/SPEC.md) (Phase 3 ở §11)
> - Hệ thống đang chạy thế nào: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · giao diện: [`DESIGN.md`](DESIGN.md)

## 1. Đang ở đâu

| Giai đoạn | Nội dung | Tình trạng |
|---|---|---|
| **Phase 1** (P0, F1–F3) | Dữ liệu sạch nhịp 1h, backfill 5 năm, attribution, dòng tiền Binance, TWR / Base 100 / drawdown / volatility | Phần lõi xong. Còn lại ở §5. |
| **Phase 2, W1–W3** (M4, M6, M7) | Tab Activity, tài sản manual, trợ lý Allocation | Xong. |
| **Phase 3, W4–W6** (N1–N9) | Earn, đối soát, peg, sổ lệnh và giá vốn, analyst kit, vốn đã nạp, nhật ký | Xong code (schema v11). N6, N7 bỏ (D21). Còn vài điểm phải kiểm trên dữ liệu thật: §2. |
| **W7, W8** (M5, M8) | Telegram, widget mobile | Hoãn (D16), §4. |

Nguyên tắc không đổi: app chỉ **mô tả** hiện trạng, không phát tín hiệu mua/bán, không đặt lệnh;
key Binance chỉ đọc; `GET` không gọi mạng; gateway public không thêm route.

## 2. Còn phải kiểm trên dữ liệu thật

| Việc | Kiểm gì |
|---|---|
| N1.2 | APR thực nhận 7d của USDC. AC 5.0% ±0.2 pp giữ nguyên (D20) và có nguy cơ không đạt: reward giảm từ ~7 xuống ~1.5 USDC/ngày từ 23/11, chưa rõ nguyên nhân. Cửa sổ đủ điều kiện đầu tiên: ~2026-12-01. |
| N4 | Tỷ lệ "tracked" của từng coin có hợp lý không; 33 lần nạp crypto có giá ngày nạp đúng không; stderr của `record` có `warning: binance convert history` không. |
| N8 | Đường nét đứt "vốn đã nạp" trên chart: chưa được thấy vẽ thật (dữ liệu thử có NAV đứng yên). Xem ở khoảng 1M hoặc 1Y. |
| N5 | Notebook mẫu chưa chạy thử (cần pandas, matplotlib cài riêng). |
| Sao lưu | Bản sao cục bộ đã có test. Phần đẩy lên cloud qua `rclone` chưa chạy với tài khoản thật: sau khi đặt `backup.remote`, bấm "Back up now" trong Settings và xem dòng "Off-site". Thử khôi phục một bản ít nhất một lần. |

## 3. Ứng viên cải thiện, chưa xếp lịch

Rút từ các giới hạn đã biết của Phase 3. Lấy ra làm khi có lý do cụ thể.

| Việc | Vì sao | Cỡ |
|---|---|---|
| Heartbeat khi cron dừng (M5.5, đang hoãn) | Snapshot bị lỡ là mất vĩnh viễn; cảnh báo "stale" chỉ thấy khi mở app. | S |
| Dòng tiền on-chain (F2.5) | Mở khoá ba thứ: "Net deposited" đúng cho ví nạp thẳng từ ngoài, đối soát ngoài Binance, giá vốn cho coin mua on-chain. | M/L |
| Cặp đã huỷ niêm yết | Coin từng mua bằng BUSD thiếu giá vốn. Lấy danh sách cặp từ `exchangeInfo` thay vì ticker hiện tại. | S |
| Hiệu năng dài hạn | Trên DB 10 năm: Performance ALL 2.9 giây, Cost basis 1.3 giây, Why it moved 1Y 1.2 giây. Chưa cần trong vài năm. | S/M |
| APR thực nhận theo thời gian | Hiện là bình quân đơn giản của snapshot; lệch nếu cron chạy không đều. | S |

Đã bỏ (D21): N6 kế hoạch giải ngân + stress, N7 hiển thị EUR. Ghi chú để mở lại ở SPEC §11.9.

## 4. Đang hoãn (D16)

Giữ nguyên spec và AC trong [`docs/SPEC.md`](docs/SPEC.md) §9.2, §9.5; không làm cho tới khi Jean mở lại.

| Wave | Task | Nội dung | Ghi chú khi mở lại |
|---|---|---|---|
| **W7** | M5.1–M5.5 | Telegram: digest 08:00, cảnh báo biến động > 5%, heartbeat cron | Nên làm sớm: dữ liệu mất do cron dừng là mất vĩnh viễn, và badge của N2 chỉ thấy khi mở app |
| **W8** | M8.1–M8.3 | Gateway `/summary` + widget iOS | Đổi bất biến bảo mật của gateway (D11): làm sau cùng |

Bị chặn theo: M5.6, M5.7, M7.6 (cần W7) · M8.4, M8.5 (cần W8).

## 5. Việc còn mở của Phase 1–2, chưa xếp lịch

Lấy ra làm khi có lý do cụ thể; chi tiết và AC ở SPEC, trạng thái ở LEDGER.

| Nhóm | Task | Ghi chú |
|---|---|---|
| Dòng tiền on-chain | F2.5, F2.9, F2.14 | Hiện chỉ Binance và tài sản manual sinh flow. Chuyển ví ↔ ví on-chain chưa được nhận diện. |
| Attribution v2 | F1.6, F1.8, F1.9 | F1.8 (bucket `trade`) nay làm được: N4 đã có trade thật. |
| Rủi ro & hiệu suất | F3.T2, F3.R3–R6, F3.B2, F3.B4, F3.C1–C3 | MWR/XIRR, Sharpe, Sortino, VaR, SPX, β, concentration. Cần ≥ 30 ngày dữ liệu ngày mới có nghĩa. |
| Hiển thị PnL | F3.P2, F2.12 | Endpoint PnL đã có, chưa có thẻ UI. N8 (dòng "Net deposited · gain") đã phủ phần lớn nhu cầu này. |
| Hiệu năng RPC | P0.7 | Multicall3. Chỉ cần khi RPC public bắt đầu giới hạn tốc độ. |
| Lặt vặt (P3) | F2.13, F3.U2, M4.7, M6.5, M7.7 | Test quyền đọc lịch sử, CLI report, badge tab, watch-only BTC, sửa mục tiêu trong Settings. |
