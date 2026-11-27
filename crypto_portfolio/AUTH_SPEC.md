# AUTH_SPEC — Mở dashboard ra Internet (để dành, chưa triển khai)

> **Trạng thái:** `[ ] BACKLOG`. Cập nhật 2026-11-26 sau khi deploy thật.
> **Quyết định hiện tại:** **không mở public.** Dashboard bind `0.0.0.0:8088` (qua
> `crypto-web.service`), truy cập bằng token qua 2 đường, không cần `tailscale serve`
> /`ts.net` (sẽ đụng mapping cổng 443 đang dùng cho hermes gateway ở 9119):
>   - **LAN nhà:** `http://192.168.1.66:8088`
>   - **Tailscale (mọi nơi, không cần ở nhà):** `http://100.123.246.52:8088`
>   Cả 2 đều là HTTP thường (không TLS) + token Bearer/session — chấp nhận được vì cả LAN
>   lẫn tailnet là mạng riêng tin cậy, không phải Internet mở. Xem [README](README.md#web-dashboard)
>   và `deploy/crypto-web.service`.
> **Khi nào mở lại tài liệu này:** khi cần xem dashboard từ thiết bị không cài được Tailscale
> và không ở nhà (khách, điện thoại công ty, v.v.).
> **Mục tiêu khi đó:** `app.tytony.link` truy cập được từ mọi nơi, không bật VPN, mà bề mặt
> public chỉ đọc được số dư, không sửa được cấu hình.

---

## 1. Nền đã có sẵn (làm xong ngày 2026-11-26)

Phần này không phải việc của spec nữa, nhưng là điểm xuất phát:

| Hạng mục | Hiện trạng |
|---|---|
| Token | `main.py serve` từ chối bind ngoài loopback khi chưa đặt `PORTFOLIO_WEB_TOKEN`. Đã đặt trong `.env` (0600, git-ignored). |
| Service | `crypto-web.service` (systemd --user, `linger` bật), bind `0.0.0.0:8088`, `Restart=on-failure`. |
| Truy cập | LAN (`192.168.1.66:8088`) và Tailscale IP (`100.123.246.52:8088`) trực tiếp — không dùng `tailscale serve`/HTTPS `ts.net` để không đụng mapping cổng 443 hiện có của hermes gateway (`127.0.0.1:9119`). |
| Cookie | Phiên đã ký `portfolio_session` (HMAC theo token, 90 ngày, HttpOnly, SameSite=Strict, `Secure` khi https). Không chứa token. |
| Brute force | Credential sai bị giới hạn 20 lần / 5 phút / client, sau đó 429. |
| Token trên URL | `/?token=` đã bỏ. |
| Secret Binance | API chỉ trả bản đã mask (`****wxyz`); secret không bao giờ rời server. |
| Dữ liệu | `config.yaml` và `portfolio_history.db` mode 0600. |
| Gateway công khai | `api.tytony.link`/`api.tito.dpdns.org` là **service riêng** (`crypto-api-gateway.service`, cổng 8090, đọc thẳng `portfolio_history.db`) — không đi qua `web_api.py`/8088, không bị ảnh hưởng bởi auth ở trên. |

## 2. Kiến trúc đề xuất

```
Thiết bị (Safari / Chrome / PWA)
      │ HTTPS
      ▼
Cloudflare Access  ── Google OAuth hoặc OTP email, allowlist đúng 1 email, phiên 30 ngày
      │ Cloudflare Tunnel (outbound-only, không mở cổng inbound)
      ▼
web_api.py  127.0.0.1:8088
      │  • kiểm chữ ký JWT `Cf-Access-Jwt-Assertion`
      │  • request đến qua Cloudflare ⇒ CHỈ ĐỌC (chặn mọi ghi vào /api/settings*)
      ▼
Tailscale (giữ nguyên) ── đường quản trị: sửa Binance key, ví, và cứu hộ khi mất Cloudflare
```

Một lớp xác thực ở rìa mạng, cộng với việc bề mặt public không ghi được gì. Kẻ vượt qua
Cloudflare Access cũng chỉ xem được số dư.

## 3. Việc cần làm

### 3.1 Cloudflare (không cần code)
1. Thêm public hostname `app.tytony.link` → `http://127.0.0.1:8088` vào tunnel hiện có.
2. Tạo Access application cho hostname đó: allowlist đúng một email, phiên 30 ngày.
3. Rule **Bypass** cho `/manifest.json` và `/static/icons/*` (trình duyệt tải manifest và icon
   không kèm cookie, thiếu bypass thì *Add to Home Screen* hỏng). Lưu ý đường dẫn icon thật là
   `/static/icons/*`, không phải `/icons/*`.

### 3.2 Backend (`web_api.py`)
1. **Kiểm JWT:** chữ ký RS256 theo JWKS của team
   (`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, cache theo `kid`), cùng với
   `aud` (AUD tag của application), `iss`, `exp` và claim `email` khớp allowlist. Lỗi ở bước
   nào cũng từ chối (fail closed). Cần thêm dependency `PyJWT[crypto]`.
2. **Quy tắc vào cửa:** JWT Cloudflare hợp lệ **hoặc** token/cookie phiên hiện có. Nhờ vậy
   đường Tailscale vẫn chạy khi Cloudflare gặp sự cố.
3. **Chỉ đọc qua Cloudflare:** request xác thực bằng JWT bị từ chối ở `POST /api/settings`,
   `/api/settings/allocation`, `/api/settings/test-binance`, `/api/settings/test-wallet`.
   `GET /api/settings` vẫn cho phép vì đã mask.
4. **Tắt trên hostname public:** `/api/docs`, `/api/openapi.json`, và `POST /api/auth`.
5. **`POST /api/portfolio/refresh`:** cho phép nhưng thêm rate limit, vì nó gọi Binance và RPC.

### 3.3 Frontend (`static/app.js`)
Khi phiên Access hết hạn, `fetch('/api/…')` nhận redirect sang trang đăng nhập khác origin và
hỏng ở tầng mạng, app sẽ báo nhầm "server unreachable". Cần cho `api()` nhận ra trường hợp
này và điều hướng lại toàn trang để Cloudflare hiện màn hình đăng nhập.

### 3.4 Phải thử trên máy thật trước khi coi là xong
- Đăng nhập Google OAuth bên trong PWA đã *Add to Home Screen* trên iOS (chế độ standalone).
  Nếu trục trặc thì chuyển sang OTP email.
- Service worker và offline shell vẫn hoạt động sau lớp Access.
- Hết phiên giữa chừng: app tự đưa về màn hình đăng nhập, không kẹt ở shell đã cache.

## 4. Đã cân nhắc và không làm

| Ý tưởng (bản spec cũ) | Lý do bỏ |
|---|---|
| Passkey / WebAuthn đăng nhập trong app | Access với Google OAuth + 2FA đã chống phishing. Thêm WebAuthn là nhiều code, một kho credential và một luồng khôi phục riêng, cho đúng một người dùng. |
| Step-up Face ID cho Settings | Thay bằng "public chỉ đọc" ở mục 3.2: đạt cùng mục tiêu mà không cần code xác thực. |
| Mã hoá AES-GCM cho Binance key | Khoá giải mã nằm cùng máy với dữ liệu nên gần như không thêm bảo vệ. File 0600 là đủ. |
| Mật khẩu + TOTP dự phòng | Tailscale đã là đường cứu hộ. Thêm một đường đăng nhập là thêm bề mặt tấn công. |
| Cảnh báo Telegram | Access đã có log đăng nhập. Có thể thêm sau nếu thấy cần. |

Xét lại passkey step-up nếu sau này cần **sửa Settings từ xa mà không có Tailscale**.

## 5. Câu hỏi còn mở (trả lời trước khi bắt tay làm)

1. Có cần sửa Settings từ ngoài Tailscale không? Mặc định của spec này: **không**.
2. Nút Refresh trên hostname public: cho phép kèm rate limit (mặc định), hay chặn hẳn?
3. Phương thức đăng nhập Access: Google OAuth hay OTP email? Tuỳ kết quả thử ở mục 3.4.

## 6. Lưới an toàn cuối cùng (áp dụng ngay cả hôm nay)

Cài đặt phía Binance, độc lập với mọi thứ ở trên:
1. API key chỉ bật **Enable Reading**.
2. Không bật **Enable Withdrawals** hay **Enable Spot & Margin Trading**.
3. Bật **IP whitelist** trỏ về IP của máy chủ.

Với ba điều này, kịch bản xấu nhất khi mọi lớp bị vượt qua là lộ số dư và địa chỉ ví, không
phải mất tiền.
