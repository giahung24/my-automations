# AUTH_SPEC — Giải pháp Xác thực & Bảo mật Web Dashboard (Dự phòng triển khai sau)

> **Trạng thái:** `[ ] BACKLOG` (Tài liệu kiến trúc lưu trữ, để dành triển khai khi cần mở web ra Internet không qua Tailscale).  
> **Mục tiêu:** Mở dashboard PWA (`0.0.0.0:8088`) ra Internet an toàn tuyệt đối, truy cập mọi nơi bằng điện thoại/laptop mà **không cần bật VPN Tailscale**, trải nghiệm mượt mà một chạm (Face ID).

---

## 1. Triết lý kiến trúc: Mô hình 2 lớp (Defense-in-Depth)

Không mở port trực tiếp trên router/VPS. Toàn bộ traffic phải đi qua Cloudflare Tunnel (`cloudflared`) kết hợp 2 lớp xác thực:

```
Thiết bị (Safari / Chrome / PWA)
      │
      ▼ (HTTPS)
[Lớp 1: Edge] Cloudflare Zero Trust Access
      │  • Xác thực Google OAuth (email Jean) + 2FA
      │  • Phiên 30 ngày (không bị hỏi lại liên tục)
      │  • Chặn 100% scanner/bot trước khi request chạm máy chủ
      ▼ (Cloudflare Tunnel - Outbound only)
FastAPI Backend (127.0.0.1:8088)
      │  • Xác thực chữ ký JWT (`Cf-Access-Jwt-Assertion`)
      ▼
[Lớp 2: App] Passkey / WebAuthn (FIDO2)
      │  • Chạm Face ID / Touch ID một chạm
      │  • Cookie phiên HttpOnly, Secure, SameSite=Strict
      ▼
[Step-up Auth] Màn hình Settings
         • Xem/Sửa Binance Key đòi xác thực Face ID lại (thời hạn 5 phút)
```

---

## 2. Chi tiết triển khai từng lớp

### Lớp 1: Cloudflare Zero Trust Access (Rìa mạng)
1. **Hostname:** Thêm `app.tytony.link` vào Cloudflare Tunnel trỏ về `http://localhost:8088`.
2. **Access Policy:**
   * Chỉ cho phép danh sách trắng: đúng email Gmail cá nhân của Jean.
   * Phương thức đăng nhập: Google OAuth hoặc One-Time PIN qua email.
   * Session Duration: **30 ngày**.
3. **Lưu ý tương thích PWA trên iOS/Android:**
   * Tạo rule **Bypass** riêng cho các asset tĩnh của PWA: `/manifest.json`, `/sw.js`, `/icons/*` để tính năng *Add to Home Screen* không bị lỗi auth.
   * Backend FastAPI thêm middleware kiểm tra chữ ký JWT `Cf-Access-Jwt-Assertion` từ Cloudflare gửi xuống để đảm bảo request không bị bypass.

---

### Lớp 2: Passkey (WebAuthn) tại App & Step-up Settings
1. **Đăng nhập một chạm:**
   * Sử dụng chuẩn WebAuthn (FIDO2). Đăng ký thiết bị chính (iPhone, Mac) lưu vào iCloud Keychain / Google Password Manager.
   * Không lưu mật khẩu trên server, chống phishing tuyệt đối.
2. **Step-up Authentication cho Settings:**
   * Màn hình chính xem tài sản: Đã vào app là xem được ngay.
   * Riêng thao tác vào **Settings để xem / cập nhật Binance API Key & Secret**: Yêu cầu xác thực lại sinh trắc học (Face ID) trong vòng 5 phút gần nhất.
   * Key lưu trong cơ sở dữ liệu được mã hoá (AES-GCM), giao diện web chỉ hiển thị 4 ký tự cuối (ví dụ: `****a1b2`).

---

### 3. Kênh dự phòng & Cảnh báo an ninh
1. **Dự phòng (Break-glass):**
   * Giữ nguyên cổng nội bộ trên mạng Tailscale để truy cập quản trị cứu hộ nếu lỡ mất quyền truy cập Cloudflare.
   * Hỗ trợ mật khẩu mạnh + TOTP (Google Authenticator) làm phương án đăng nhập phụ.
2. **Cảnh báo Telegram:**
   * Bot Telegram tự động gửi thông báo tức thời khi:
     - Có phiên đăng nhập mới từ IP lạ.
     - Có yêu cầu mở màn hình Settings hoặc thay đổi API Key.
     - Có thiết bị Passkey mới được thêm vào.

---

### 4. Nguyên tắc bảo vệ tài sản sống còn (Binance API Key)
Dù hacker có vượt qua được toàn bộ các lớp bảo mật trên, tài sản vẫn được bảo toàn nhờ 2 nguyên tắc cài đặt tại Binance:
1. **Chỉ bật quyền `Enable Reading` (Read-only)**.
2. **Tuyệt đối KHÔNG BẬT `Enable Withdrawals` (Rút tiền)** hay `Enable Spot & Margin Trading`.
3. **Bật IP Whitelist** trên Binance trỏ về IP tĩnh/IP mạng nhà của máy chủ.
*Kịch bản xấu nhất xảy ra:* Chỉ bị lộ số dư, **tuyệt đối không ai có thể rút hay di chuyển tiền của anh**.
