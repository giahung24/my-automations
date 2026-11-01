# Binance Spot Grid check

Kiểm tra regime ETH/USDC và tính params cho Spot Grid bot theo [SPEC.md](SPEC.md). Chỉ đọc dữ liệu public, không đặt lệnh.

```bash
uv venv && uv pip install -r requirements.txt
python grid_check.py --pair ETHUSDC --fee 0.001 --days 21 --capital 1000
python -m pytest
```

Kết quả in ra màn hình và lưu vào `runs/<pair>_<date>.json` (không commit).
