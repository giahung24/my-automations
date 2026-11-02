# Binance Spot Grid check

Kiểm tra regime và tính params cho Spot Grid bot theo [SPEC.md](SPEC.md), mặc định cho ETH, BTC, SOL, LINK /USDC. Chỉ đọc dữ liệu public, không đặt lệnh.

```bash
uv venv && uv pip install -r requirements.txt
python grid_check.py                                   # 4 cặp mặc định
python grid_check.py --pairs ETHUSDC SOLUSDC --capital 500
python -m pytest
```

Kết quả: danh sách JSON trên stdout, một dòng tóm tắt mỗi cặp trên stderr, và `runs/<pair>_<date>.json` cho từng cặp (không commit).

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
