# Spec — `grid_check.py` (Binance Spot Grid, ETH/USDC và các cặp USDC khác)

Trích từ tài liệu gốc [Hướng dẫn setup Binance Spot Grid Bot (ETH/USDC)](https://claude.ai/artifact/2y2CQUDpSLAXB5b3kW29TZ) (bản 2026-10-23), chỉ giữ phần script cần. Bước 3 (setup trên Binance) và Bước 4 (theo dõi) là việc làm tay, xem tài liệu gốc.

**Nguyên tắc:** mọi con số và quyết định go/no-go do script tính theo quy tắc cố định. LLM chỉ chạy script, đọc output và giải thích. Script không đặt lệnh, không cần API key.

## Đầu vào

- CLI: `--pairs ETHUSDC BTCUSDC SOLUSDC LINKUSDC --fee 0.001 --days 21 --capital 1000` (mặc định 4 cặp này; `--pair` vẫn dùng được).
- Chỉ dùng endpoint dữ liệu public `https://data-api.binance.vision/api/v3` (klines, exchangeInfo).
- `f` = phí mỗi chiều (mặc định khoảng 0.1%; thấp hơn nếu trả phí bằng BNB).

## Bước 1 — Kiểm tra regime

Grid chỉ được bật khi thị trường đang đi ngang hoặc trend yếu, không trong downtrend và volatility không bùng nổ. Script trả về một trong ba kết luận: **GO**, **CAUTION** hoặc **NO-GO**.

**Dữ liệu:** 300 nến ngày và 60 ngày nến 4h của ETH/USDC (dùng ETH/USDT nếu lịch sử USDC quá ngắn), cộng 300 nến ngày BTC/USDC cho bộ lọc thị trường chung. Chỉ dùng nến đã đóng; bỏ nến cuối cùng vì nó còn đang chạy.

| Chỉ số | Cách tính | Đo điều gì |
| --- | --- | --- |
| Vị trí so với SMA200 | Giá đóng cửa / SMA200 ngày − 1, cho ETH và BTC | Xu hướng dài hạn |
| Độ dốc SMA50 | % thay đổi của SMA50 trong 20 ngày | Trend trung hạn đang lên, xuống hay phẳng |
| ADX(14) ngày | Chỉ số Wilder chuẩn | Sức mạnh trend, không quan tâm hướng |
| Efficiency Ratio 30 ngày | \|giá nay − giá 30 ngày trước\| / tổng \|biến động từng ngày\| | Gần 0 = đi ngang, gần 1 = đi thẳng một chiều |
| Volatility 30 ngày | Độ lệch chuẩn log-return ngày × √365 | Biên độ dao động hiện tại |
| Tỉ lệ volatility | Vol 30 ngày / vol 180 ngày | Volatility đang bùng nổ hay ổn định |
| ATR(14) ngày | % so với giá | Dao động trung bình mỗi ngày |
| Vùng 30 ngày | Đỉnh, đáy và vị trí giá trong vùng (0–100%) | Giá đang ở giữa hay sát biên |

**Quy tắc kết luận** (áp dụng theo thứ tự, dừng ở quy tắc đầu tiên khớp):

| Kết luận | Điều kiện |
| --- | --- |
| NO-GO (downtrend) | ETH dưới SMA200 và SMA50 dốc xuống, hoặc BTC dưới SMA200 |
| NO-GO (trend mạnh) | ADX > 30 hoặc Efficiency Ratio > 0.5 — dùng bot trend thay vì grid |
| NO-GO (volatility bùng nổ) | Tỉ lệ volatility > 1.5 |
| GO | ADX < 22, Efficiency Ratio < 0.35, \|độ dốc SMA50\| < 5%, giá trong 25–75% của vùng 30 ngày |
| CAUTION | Mọi trường hợp còn lại: chỉ chạy với một nửa vốn dự kiến và vùng giá rộng hơn |

Các ngưỡng này là điểm khởi đầu hợp lý, chưa được kiểm định. Trước khi tin chúng, backtest một grid có bộ lọc này trên lịch sử ETH từ 2018, so với chỉ cầm ETH.

**Output:** một file JSON gồm thời điểm chạy, giá hiện tại, tất cả chỉ số trên, kết luận, lý do (quy tắc nào khớp) và bộ params ở Bước 2 nếu kết luận không phải NO-GO.

## Bước 2 — Công thức tính params

Ký hiệu: P = giá hiện tại, σ = volatility ngày (vol 30 ngày / √365), f = phí mỗi chiều, H = số ngày dự định chạy bot (14–30).

| Param | Công thức | Ghi chú |
| --- | --- | --- |
| Kiểu lưới | Geometric | Phí tính theo %, nên khoảng cách theo % giữ lãi mỗi lưới đều |
| Giá dưới (L) | min(P × e^(−kσ√H), đáy 30 ngày × (1 − 0.5 × ATR%)) | k = 1.5 khi GO, 2 khi CAUTION |
| Giá trên (U) | max(P × e^(kσ√H), đỉnh 30 ngày × (1 + 0.5 × ATR%)) | Vùng rộng hơn = ít bị phá hơn nhưng ít khớp lệnh hơn |
| Khoảng cách lưới (s) | ATR(14) nến 4h tính theo %, kẹp trong [max(0.6%, 6f), ATR ngày% / 2] | Dưới sàn thì phí ăn hết lãi; trên trần thì giá hiếm khi chạm lưới |
| Số lưới (N) | round(ln(U/L) / ln(1 + s)) | Kiểm tra N nằm trong giới hạn giao diện Binance |
| Lãi ròng mỗi lưới | (1 − f)(1 + s) − 1 − f ≈ s − 2f | Phải ≥ 2 × phí khứ hồi, nếu không thì NO-GO |
| Vốn đầu tư | Tối đa 10–20% số vốn crypto của bạn; một nửa nếu CAUTION | Mỗi lệnh (vốn / N) phải lớn hơn mức tối thiểu của sàn với dư địa |
| Stop-loss | L × (1 − ATR ngày%) | Bắt buộc |
| Take-profit | Tùy chọn: U × (1 + ATR ngày%) | Giá phá lên = thị trường chuyển sang trend |
| Trigger price | Không dùng, trừ khi giá đang sát biên vùng | Chờ giá về giữa vùng thì tốt hơn |
| Trailing up | Tắt | Tránh đuổi giá lên rồi kẹt ở vùng cao |

**Kiểm tra cuối trước khi xuất params:** lãi ròng mỗi lưới ≥ 0.4% (với f = 0.1%), N không vượt giới hạn, khoảng cách từ P tới stop-loss là mức lỗ chấp nhận được nếu toàn bộ vốn đã chuyển thành ETH.

Công thức lãi mỗi lưới geometric của Binance: (1 − phí) × (giá trên / giá dưới)^(1/số lưới) − 1 − phí. Ví dụ của Binance: vùng 400–450, 5 lưới, phí 0.1% → khoảng 2.18% mỗi lưới ([FAQ tham số](https://www.binance.com/en/support/faq/binance-spot-grid-trading-parameters-688ff6ff08734848915de76a07b953dd)).

## Ví dụ minh họa (dùng làm test)

Số liệu giả định, không phải giá hiện tại.

**Đầu vào:** P = 3,000 USDC; vol 30 ngày 55%/năm (σ ≈ 2.88%/ngày); ATR ngày 3.2%; ATR 4h 1.1%; vùng 30 ngày 2,750–3,250; ADX 18; Efficiency Ratio 0.25; ETH và BTC trên SMA200; phí 0.1%; chạy 21 ngày; vốn 1,000 USDC. Kết luận: **GO** (k = 1.5).

| Param | Kết quả | Cách ra số |
| --- | --- | --- |
| Giá dưới | 2,461.5 USDC | 3,000 × e^(−1.5 × 2.88% × √21); rộng hơn đáy 30 ngày có buffer (2,706) |
| Giá trên | 3,656.5 USDC | 3,000 × e^(1.5 × 2.88% × √21) |
| Khoảng cách lưới | 1.1% | ATR 4h 1.1% nằm trong [0.6%, 1.6%] |
| Số lưới | 36 | ln(3,656.5 / 2,461.5) / ln(1.011) |
| Lãi ròng mỗi lưới | ≈ 0.90% | 0.999 × 1.011 − 1 − 0.001 |
| Tiền mỗi lệnh | ≈ 27.8 USDC | 1,000 / 36 |
| Stop-loss | ≈ 2,383 USDC | 2,461.5 × (1 − 3.2%) |
| Take-profit | Bỏ trống hoặc ≈ 3,773 USDC | 3,656.5 × (1 + 3.2%) |
| Trailing up | Tắt | |

**Rủi ro tệ nhất:** nếu giá rơi thẳng xuống stop-loss, bot đã mua đủ mọi lưới và giữ khoảng 0.353 ETH, trị giá khoảng 841 USDC tại 2,383, tức lỗ khoảng 16% vốn.

Output JSON mẫu:

```json
{
  "pair": "ETHUSDC",
  "as_of": "YYYY-MM-DDT00:00:00Z",
  "price": 3000.0,
  "metrics": {"pair_vs_sma200": 0.08, "btc_vs_sma200": 0.12, "sma50_slope_20d": 0.02,
              "adx14": 18, "efficiency_ratio_30d": 0.25, "vol30_ann": 0.55,
              "vol_ratio_30_180": 0.9, "atr14_daily_pct": 0.032, "atr14_4h_pct": 0.011,
              "range30": [2750, 3250], "range30_position": 0.5},
  "verdict": "GO",
  "reason": "ADX<22, ER<0.35, |slope|<5%, price mid-range",
  "params": {"mode": "geometric", "lower": 2461.5, "upper": 3656.5, "grids": 36,
             "net_profit_per_grid": 0.009, "investment_usdc": 1000,
             "stop_loss": 2383, "take_profit": null, "trailing_up": false},
  "worst_case_loss_pct": -0.16
}
```

## Prompt cho agent (nguyên văn từ tài liệu gốc)

```text
You are helping me decide whether to run a Binance Spot Grid bot and with which parameters.
Follow the attached guide (sections "Bước 1" and "Bước 2") exactly.

First run only:
- Write grid_check.py (Python, pandas, requests). Use only public Binance REST klines
  (no API key). Drop the last, still-forming candle.
- Implement every metric, the verdict rules in order, and the parameter formulas.
- Inputs: --pair ETHUSDC --fee 0.001 --days 21 --capital 1000.
- Output: the JSON structure shown in the guide, saved to runs/<pair>_<date>.json.
- Add unit tests for ADX, efficiency ratio, grid count and net profit per grid,
  including the worked example in the guide (expect lower 2461.5, upper 3656.5, 36 grids).

Every run:
1. Run the script for the requested pair. Do not compute or estimate any number yourself.
2. Report: verdict and the rule that triggered it, the key metrics, the parameters,
   the worst-case loss at stop-loss, and anything abnormal (missing candles, a parameter
   outside its bounds, net profit per grid below 0.4%).
3. If the verdict is NO-GO, say so plainly and do not suggest parameters.
4. Never place orders or ask for API secrets. I create the bot manually in the Binance UI.
5. Keep the report under 15 lines and in Vietnamese.
```

## Ghi chú triển khai (kiểm tra ngày 2026-11-01)

Đã xác minh:

- `api.binance.com` truy cập được từ máy này, không cần key.
- ETHUSDC và BTCUSDC có nến ngày từ 2018-12-15, đủ 300 nến: không cần fallback sang ETHUSDT.
- Bộ lọc sàn ETHUSDC (`/api/v3/exchangeInfo`): `minNotional` 5 USDC, `tickSize` 0.01, `stepSize` 0.0001 ETH. Lấy động từ endpoint này thay vì hardcode.
- Tính lại ví dụ minh họa: L ≈ 2,461.4, U ≈ 3,656.5, N = 36, lãi ròng 0.899%, SL ≈ 2,382.6, giữ 0.352 ETH, lỗ tệ nhất −16.1%. Khớp tài liệu trong sai số làm tròn (σ làm tròn 2.88%), nên test dùng sai số tương đối khoảng 0.1%, không so bằng tuyệt đối.

Điểm tài liệu để ngỏ, chọn mặc định sau (sửa ở đây nếu khác ý):

1. **"SMA50 dốc xuống"** trong NO-GO (downtrend): hiểu là `sma50_slope_20d < 0`.
2. **Giới hạn N của Binance**: `MAX_GRIDS = 170` theo [FAQ Spot Grid](https://www.binance.com/en/support/faq/what-is-spot-grid-trading-and-how-does-it-work-d5f441e8ab544a5b98241e00efb3a4ab) ("up to 170 individual grids"); vượt thì cảnh báo. Xác nhận trên giao diện.
3. **"Dư địa" trên mức lệnh tối thiểu**: mặc định vốn/N ≥ 2 × `minNotional`.
4. **Vốn khi CAUTION**: `investment_usdc = capital × 0.5`.
5. **Lãi ròng mỗi lưới xuất ra**: tính từ khoảng cách thật sau khi làm tròn N, tức (U/L)^(1/N), cùng công thức Binance hiển thị, để so ở Bước 3 (lệch > 0.05 điểm %). Với ví dụ: 0.904% thay vì 0.899%.
6. **Lỗ tệ nhất**: vốn chia đều N lệnh; các mức trên P mua ở giá P lúc tạo bot, các mức dưới P mua tại mức của nó. Lượng ETH × SL / vốn − 1.
7. **Vùng 30 ngày**: đỉnh/đáy theo high/low của 30 nến ngày đã đóng; vị trí = (P − đáy) / (đỉnh − đáy).
8. **Volatility**: độ lệch chuẩn mẫu (ddof=1) của 30 (và 180) log-return ngày gần nhất.
9. **Tải dữ liệu**: 3 request klines (ETH 1d, ETH 4h, BTC 1d) chạy song song; mỗi request ≤ 1000 nến nên không cần phân trang.
10. `.gitignore` của repo bỏ qua `*.json`, nên `runs/*.json` không bị commit (đúng ý) và fixture test không được để dạng JSON.

Quyết định thêm khi code (`grid_check.py`):

11. **Giá P**: giá đóng cửa của nến ngày đã đóng gần nhất; `as_of` = thời điểm nến đó đóng. Mọi chỉ số ngày cùng một mốc.
12. **Sàn > trần khoảng cách lưới** (ATR ngày < 1.2%): dùng sàn, thêm cảnh báo. Sàn bảo vệ khỏi phí nên được ưu tiên.
13. **Làm tròn N**: N = round(...) nhưng không vượt mức làm khoảng cách thật xuống dưới sàn. Nếu không, round() lên có thể đẩy lãi ròng xuống 0.39% và ra NO-GO oan.
14. **Ngưỡng lãi ròng**: so lãi ròng làm tròn 0.01% (như Binance hiển thị) với 4f. Ngay tại sàn s = 6f, lãi ròng chính xác là 4f − 6f² = 0.3994%, so tuyệt đối sẽ luôn trượt.
15. **JSON thêm 2 trường**: `params.take_profit_optional` (vì agent không được tự tính số) và `warnings` (nến thiếu, N vượt giới hạn, lệnh quá nhỏ, sàn > trần).
16. **Thiếu dữ liệu**: chỉ số nào ra NaN thì cặp đó báo lỗi, không để NaN rơi xuống CAUTION.
17. **ADX/ATR**: Wilder có seed SMA; đã đối chiếu với thư viện `ta` trên dữ liệu thật ETHUSDC (khớp tới 3 chữ số thập phân).

Mở rộng nhiều cặp (2026-11-01):

18. **Cặp mặc định**: ETHUSDC, BTCUSDC, SOLUSDC (nến từ 2021-09), LINKUSDC (từ 2019-01); cả bốn đủ 300 nến ngày, `minNotional` 5 USDC. Các cặp chạy song song; một cặp lỗi (sai tên, thiếu dữ liệu) không chặn các cặp khác, nhưng exit code = 1.
19. **Ngưỡng regime** được đặt cho ETH và chưa kiểm định cho BTC/SOL/LINK. Params tự co giãn theo volatility của từng cặp. `--capital` là vốn cho mỗi cặp, không phải tổng.
20. **Đổi tên chỉ số**: `eth_vs_sma200` → `pair_vs_sma200` (giá của cặp đang xét so với SMA200 của nó). Bộ lọc BTC vẫn là `btc_vs_sma200`.
21. **Endpoint**: `api.binance.com` trả 451 cho IP ở Mỹ (cloud, GitHub Actions); `data-api.binance.vision` là endpoint dữ liệu public của Binance, cùng định dạng.
22. **Giá trong params** làm tròn 0.01: hợp với cặp giá > ~1 USDC (LINKUSDC có tick 0.001 nên 0.01 vẫn hợp lệ). Coin rẻ hơn cần làm tròn theo `tickSize`.

**Khi Claude chạy:** `python grid_check.py` (mặc định 4 cặp). Stdout là danh sách JSON, stderr là một dòng tóm tắt mỗi cặp. Báo cáo theo prompt ở trên cho cả lần chạy: một dòng kết luận mỗi cặp, chi tiết params chỉ cho cặp GO hoặc CAUTION.
