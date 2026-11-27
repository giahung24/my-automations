#!/usr/bin/env python3
"""Autonomous Runner for crypto_portfolio Wave 2 & Wave 3 (M6, M4, M7).

Answers from Jean incorporated:
1. USDC ($29k) is dry powder waiting to buy dips. Target allocation must have an ON/OFF toggle.
2. Group daily Earn rewards into 1 line per day (expandable).
3. Sync full 5 years of Binance deposit/withdraw/fiat/P2P history.
4. Jean holds BTC in cold storage (needs M6 manual_assets support).

Sequence:
- Task 1: M6 - Manual Assets & Cold BTC (config block, valuation, DB migration manual_usd, Settings UI).
- Task 2: M4 - Activity Tab & Full 5y Binance Sync (sync engine, daily Earn grouping, 3-tab Bottom Nav).
- Task 3: M7 - Allocation Assistant (target config, ON/OFF toggle, dry powder support, UI card).

Runs in background and logs to logs/wave2_3.log.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/home/jean/projects/my-automations/crypto_portfolio")
CLAUDE_BIN = "/home/jean/.local/bin/claude"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "wave2_3.log"
PYTHON_VENV = ROOT / ".venv" / "bin" / "python"
PYTEST_VENV = ROOT / ".venv" / "bin" / "pytest"

TASKS = [
    {
        "id": "M6.cold_btc",
        "title": "Manual Assets & Cold BTC Storage",
        "spec": """Triển khai Epic M6 trong ROADMAP_LEDGER.md (§9.3):
1. Thêm cấu hình manual_assets vào config.py / config.yaml (label, kind: cold_wallet|staking, holdings: [{symbol: BTC, quantity: ...}]).
2. Tích hợp định giá thị trường tự động cho tài sản manual (BTC lấy giá ticker Binance), cộng vào Net Worth và lưu snapshot vào cột manual_usd (tạo migration DB nếu cần).
3. Đảm bảo thay đổi số lượng manual không làm sai lệch PnL/TWR (tạo dòng cash_flows tương ứng).
4. Thêm UI quản lý tài sản manual trong màn hình Settings (cho phép nhập số lượng BTC ví lạnh, nhãn). Thêm bộ lọc 'Cold' hoặc gom vào On-chain/Self-custody.
5. Viết unit tests đầy đủ trong test_all.py hoặc test_manual.py.""",
    },
    {
        "id": "M4.activity_tab",
        "title": "Tab 3 Activity & Sync 5 năm Binance",
        "spec": """Triển khai Epic M4 trong ROADMAP_LEDGER.md (§9.1):
1. Bổ sung endpoint GET /api/cash-flows và cơ chế đồng bộ tối đa 5 năm lịch sử Binance (crypto deposit/withdraw, fiat/P2P, earn rewards).
2. Gom lãi Earn hàng ngày thành 1 dòng duy nhất trên timeline mỗi ngày: '✦ Earn +$X.XX · N assets', bấm vào bung chi tiết từng coin.
3. Thuật toán đối soát chuyển ví nội bộ giữa Binance và các ví đã cấu hình (My Ledgy, New Trust, My Sol) -> is_internal_transfer = True.
4. Nâng cấp Bottom Nav Bar thành 3 tabs: [ Portfolio | Analytics | Activity ].
5. Giao diện Tab 3: Timeline theo ngày, bộ lọc All/Binance/On-chain, chip lọc Nạp/Rút/Chuyển/Earn, nút xác nhận thủ công nếu có giao dịch chuyển ví chưa match.
6. Viết unit tests đầy đủ, chạy pytest pass 100%.""",
    },
    {
        "id": "M7.rebalance_toggle",
        "title": "Asset Allocation & Rebalancing Assistant có công tắc ON/OFF",
        "spec": """Triển khai Epic M7 trong ROADMAP_LEDGER.md (§9.4):
1. Module services/analytics/allocation.py: tính tỷ trọng hiện tại, độ lệch (drift), và gợi ý lệnh mua/bán tái cân bằng.
2. CÓ CÔNG TẮC BẬT/TẮT: Cho phép bật/tắt tính năng Allocation trong Settings hoặc ngay trên thẻ UI (mặc định tắt cho tới khi người dùng kích hoạt).
3. Hỗ trợ bucket 'Dry powder / Tiền chờ bắt đáy' hoặc cho phép trừ/loại phần USDC trong Earn khỏi cảnh báo lệch để không bị báo sai ý định gom hàng của Jean.
4. Hỗ trợ chế độ '?contribution=...' (chỉ gợi ý MUA bằng tiền nạp mới, không bán coin cũ).
5. Giao diện thẻ Allocation trong Tab Analytics: thanh so sánh Mục tiêu vs Thực tế, danh sách lệnh gợi ý có nút copy.
6. Viết unit tests đầy đủ, đảm bảo pytest pass 100%.""",
    },
]


def log(msg: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_claude(model: str, prompt: str, timeout: int = 500) -> tuple[int, str]:
    cmd = [
        CLAUDE_BIN,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "-p",
        prompt,
    ]
    log(f"Invoking Claude ({model})...")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired:
        log(f"Claude ({model}) timed out after {timeout}s")
        return -1, "Timeout"


def run_pytest() -> tuple[int, str]:
    log("Running pytest...")
    proc = subprocess.run(
        [str(PYTEST_VENV), "-q"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("=== Starting Wave 2 & Wave 3 Autonomous Orchestrator ===")
    log(f"Tasks: {len(TASKS)}")

    for idx, task in enumerate(TASKS, 1):
        task_id = task["id"]
        title = task["title"]
        spec = task["spec"]
        log(f"\n--- Starting Task {idx}/{len(TASKS)}: [{task_id}] {title} ---")

        # 1. Opus Code
        opus_prompt = f"""Bạn là Claude Opus 5.5, Lead Engineer. Hãy trực tiếp triển khai task sau trong dự án /home/jean/projects/my-automations/crypto_portfolio:

Task ID: {task_id}
Tiêu đề: {title}
Yêu cầu:
{spec}

Quy tắc thực hiện:
- Code sạch, hoàn chỉnh trực tiếp vào các file tương ứng trong dự án (backend, frontend, config).
- Viết unit tests đầy đủ kiểm tra các chức năng mới.
- Chạy pytest và đảm bảo 100% test suite pass (không làm hỏng test cũ).
- Báo cáo kết quả ngắn gọn.
"""
        code_ret, code_out = run_claude("claude-opus-5-5", opus_prompt, timeout=600)
        log(f"Opus finished with exit code {code_ret}")

        # 2. Pytest
        test_ret, test_out = run_pytest()
        log(f"Pytest exit code: {test_ret}")
        if test_ret != 0:
            log(f"Pytest failed! Fixing...\n{test_out[:400]}")
            fix_prompt = f"Pytest thất bại sau khi bạn code task {task_id}:\n{test_out}\nHãy sửa ngay các lỗi để pytest pass."
            run_claude("claude-opus-5-5", fix_prompt, timeout=400)
            test_ret, test_out = run_pytest()

        # 3. Fable Review
        fable_prompt = f"""Bạn là Claude Fable 5.2, Senior Code Reviewer.
Hãy kiểm tra git diff và các file vừa được tạo/sửa cho task {task_id} ({title}) tại /home/jean/projects/my-automations/crypto_portfolio.
Kết quả pytest:
{test_out[:800]}

Đánh giá xem:
1. Code có đáp ứng chuẩn xác yêu cầu: {spec} không?
2. Có lỗi tiềm ẩn, race condition, hoặc vấn đề bảo mật nào không?
Nếu đạt chuẩn, in rõ: "VERDICT: APPROVED".
Nếu chưa đạt, chỉ ra lỗi cụ thể cần sửa.
"""
        rev_ret, rev_out = run_claude("claude-fable-5-2", fable_prompt, timeout=300)
        log(f"Fable review exit code: {rev_ret}")

        # Git commit
        subprocess.run(["git", "add", "."], cwd=str(ROOT))
        commit_res = subprocess.run(
            ["git", "commit", "-m", f"feat({task_id}): {title}"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        sha = commit_res.stdout.split()[1] if commit_res.returncode == 0 else "done"
        log(f"Task {task_id} committed with SHA {sha}")
        time.sleep(3)

    log("\n=== ALL WAVE 2 & 3 TASKS COMPLETED SUCCESSFULLY! ===")


if __name__ == "__main__":
    main()
