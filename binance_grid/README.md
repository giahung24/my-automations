# Binance Spot Grid check

Kiểm tra regime và tính params cho Spot Grid bot theo [SPEC.md](SPEC.md), mặc định cho ETH, BTC, SOL, LINK /USDC. Chỉ đọc dữ liệu public, không đặt lệnh.

```bash
uv venv && uv pip install -r requirements.txt
python grid_check.py                                   # 4 cặp mặc định
python grid_check.py --pairs ETHUSDC SOLUSDC --capital 500
python -m pytest
```

Kết quả: danh sách JSON trên stdout, một dòng tóm tắt mỗi cặp trên stderr, và `runs/<pair>_<date>.json` cho từng cặp (không commit).
