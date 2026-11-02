# Binance Spot Grid check

Kiểm tra regime và tính params cho Spot Grid bot theo [SPEC.md](SPEC.md), mặc định cho ETH, BTC, SOL, LINK /USDC. Chỉ đọc dữ liệu public, không đặt lệnh. SPEC.md chứa toàn bộ tài liệu hướng dẫn gốc (artifact đã bị xóa, nội dung được khôi phục), kể cả cách setup trên Binance (Bước 3) và cách theo dõi (Bước 4).

## Giải thuật: grid đạt GO là grid như thế nào

Một cặp ra **GO** khi giá đang **đi ngang trong một vùng ổn định**. Phải đạt cùng lúc các điều kiện:

| Điều kiện | Ý nghĩa |
| --- | --- |
| Giá trên SMA200 (hoặc SMA50 không dốc xuống), BTC trên SMA200 | Không trong downtrend, kể cả của cả thị trường |
| ADX(14) < 22 và Efficiency Ratio 30 ngày < 0.35 | Trend yếu; giá đi lòng vòng chứ không đi thẳng |
| \|Độ dốc SMA50 trong 20 ngày\| < 5% | Xu hướng trung hạn gần như phẳng |
| Giá ở 25–75% vùng 30 ngày | Giá ở giữa vùng, không sát đỉnh hay đáy |
| Vol 30 ngày ≤ 1.5 × vol 180 ngày | Volatility không bùng nổ |
| Lãi ròng mỗi lưới ≥ 2 × phí khứ hồi (0.4% với phí 0.1%) | Mỗi lần khớp lệnh đủ bù phí |

Đạt một phần (không NO-GO nhưng chưa đủ GO) là **CAUTION**: chỉ dùng nửa vốn, vùng giá rộng hơn.

**Thời hạn: trung hạn, 2–4 tuần** (`--days` từ 14 đến 30, mặc định 21). Không thiết kế cho bot vài ngày hay vài tháng:

- **Vùng giá được tính cho đúng H ngày**: P × e^(±kσ√H), với k = 1.5 khi GO và k = 2 khi CAUTION. Theo mô hình random walk, khả năng giá chạm một trong hai biên trong H ngày nhiều nhất khoảng 1/4 với k = 1.5, và khoảng 1/10 với k = 2. Vùng thật có thể rộng hơn, vì luôn bao trọn vùng 30 ngày cộng một khoảng đệm.
- **Ngắn hơn (vài ngày):** mọi chỉ số đều đo trên nến ngày (ADX 14 ngày, vùng 30 ngày), nên không phản ánh nhịp trong ngày. Phí mua coin lúc tạo bot cũng chiếm tỉ trọng lớn so với số lần khớp.
- **Dài hơn (vài tháng):** regime dễ đổi, và vùng phải rộng ra theo √H nên khớp ít hơn. Vì vậy cần chạy lại kiểm tra mỗi tuần và xét lại khi hết H ngày.

**Tính chất của một grid GO:**

- **Khoảng cách lưới ≈ ATR của nến 4h**, kẹp trong [0.6%, ATR ngày / 2]. Chỉ cần một nhịp dao động cỡ một cây nến 4h là khớp được một lưới.
- **Lãi đến từ dao động, không đến từ xu hướng.** Mỗi lần giá đi qua một lưới rồi quay lại là một cặp lệnh khớp, lãi ≈ lãi ròng mỗi lưới × tiền mỗi lệnh. Giá chạy một chiều thì grid thua: giá giảm thì bot ôm toàn coin; giá tăng mạnh thì bot bán hết sớm và lãi ít hơn cầm coin.
- **Rủi ro biết trước:** stop-loss nằm dưới giá dưới thêm 1 ATR ngày. `worst_case_loss_pct` là mức lỗ nếu giá rơi thẳng xuống stop-loss: khoảng −12% với BTC, tới −30% đến −40% với coin biến động mạnh. Con số này chỉ đúng khi bật bán hết coin lúc dừng.
- **Thoát theo quy tắc** (Bước 4, `bot_check.py`): chạm stop-loss, regime NO-GO, giá vượt giá trên, hoặc hết H ngày mà không còn GO.
- **Đánh giá sau 5–10 bot:** so tổng lãi/lỗ (gồm phần chưa chốt) với cầm coin và cầm USDC. Các ngưỡng trên là quy tắc kinh nghiệm, chưa được backtest.

## Chạy

```bash
uv venv && uv pip install -r requirements.txt
python grid_check.py                                   # 4 cặp mặc định
python grid_check.py --pairs ETHUSDC SOLUSDC --capital 500
python -m pytest
```

Kết quả: danh sách JSON trên stdout, một dòng tóm tắt mỗi cặp trên stderr, và `runs/<pair>_<date>.json` cho từng cặp (không commit).

## Quét tìm cặp

```bash
python grid_scan.py                      # cặp USDC có khối lượng 24h >= 2M
python grid_scan.py --min-volume 1000000
```

Liệt kê các cặp GO rồi CAUTION, xếp theo `fit` (1 là lý tưởng, 0 là đúng ngưỡng GO), kèm khối lượng 24h và params. Đã bỏ stablecoin, token đòn bẩy và coin có lịch sử dưới 300 ngày.

## Theo dõi bot đang chạy

Ghi các bot vào `bots.json` (chỉ trên máy, không commit), lấy thông số và giờ tạo từ giao diện Binance:

```json
[{"pair": "LINKUSDC", "lower": 8.47, "upper": 16.03, "stop_loss": 7.95,
  "sell_on_stop": false, "created": "2026-11-02T08:00:00Z", "days": 21}]
```

```bash
python bot_check.py
```

Mỗi bot ra `KEEP`, `STOP` hoặc `STOPPED` theo bảng Bước 4 của tài liệu, kèm vị trí giá trong vùng và khoảng cách tới stop-loss.
