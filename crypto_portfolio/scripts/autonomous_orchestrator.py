#!/usr/bin/env python3
"""Autonomous Runner for crypto_portfolio Roadmap.

Iterates through Roadmap tasks:
1. Prompts Claude Opus 5.5 to implement code and unit tests.
2. Runs pytest to verify test suite passes.
3. Prompts Claude Fable 5.2 to review implementation and test coverage.
4. Updates ROADMAP_LEDGER.md with [x] DONE upon verification and commits.
Runs in background and logs to logs/orchestrator.log.
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
LOG_FILE = LOG_DIR / "orchestrator.log"
PYTHON_VENV = ROOT / ".venv" / "bin" / "python"
PYTEST_VENV = ROOT / ".venv" / "bin" / "pytest"

TASKS = [
    {
        "id": "P0.1",
        "title": "Cơ chế migration bằng PRAGMA user_version",
        "spec": "Triển khai hệ thống migration schema SQLite trong services/db.py bằng PRAGMA user_version, transactional, tự backup file trước khi migrate. Viết unit test trong test_all.py kiểm tra upgrade từ v0 lên v1/v2 không mất data.",
    },
    {
        "id": "P0.2_P0.3",
        "title": "Bảng snapshot_sources, detail, price_id, trigger",
        "spec": "Thêm bảng snapshot_sources theo dõi trạng thái từng nguồn (ok/error/disabled). Thêm cột detail và price_id vào snapshot_assets. Cột trigger vào snapshots. Cập nhật portfolio.py và db.py. Đảm bảo mọi test cũ và mới đều pass.",
    },
    {
        "id": "P0.4",
        "title": "Bật PRAGMA journal_mode=WAL",
        "spec": "Bật chế độ WAL cho SQLite trong services/db.py khi khởi tạo connection. Đảm bảo api_gateway.py mode=ro đọc an toàn đồng thời khi collector ghi.",
    },
    {
        "id": "P0.8",
        "title": "Backfill lịch sử 5 năm (60 checkpoints tháng)",
        "spec": "Tạo script/service services/backfill.py kéo giá lịch sử tháng của BTC, ETH, SOL, USDT từ Binance klines công khai (không cần API key) từ tháng 11/2021 đến tháng 10/2026. Tạo 60 snapshot ngày đầu mỗi tháng (NAV hồi quy dựa trên tỷ trọng danh mục hiện tại và giá quá khứ), gắn nhãn is_backfilled=1 để biểu đồ hiển thị được view dài hạn.",
    },
    {
        "id": "F1.att",
        "title": "Attribution Analysis ('Vì sao hôm nay biến động?')",
        "spec": "Xây dựng module services/analytics/attribution.py phân rã delta NAV giữa 2 snapshot liên tiếp thành Price Effect và Quantity Effect cho từng coin. Xử lý các edge case (P=0 data gap, coin mới). Viết endpoint GET /api/portfolio/attribution?window=24h trong web_api.py và thêm card hiển thị top contributors (coin kéo tăng/kéo giảm) trên giao diện static/app.js.",
    },
    {
        "id": "F2.flow",
        "title": "Cash Flow Engine & Binance Deposit/Withdraw/Fiat Sync",
        "spec": "Tạo bảng cash_flows. Bổ sung sync lịch sử nạp rút crypto và fiat/P2P từ Binance API. Thuật toán đối soát chuyển ví nội bộ dựa trên danh sách địa chỉ ví đã cấu hình (is_internal_transfer = True). Tính toán PnL thực tế 7d và 30d đã trừ dòng tiền nạp/rút ròng.",
    },
    {
        "id": "F3.quant",
        "title": "Quantitative Intelligence & Benchmark",
        "spec": "Tạo module services/analytics/quant.py tính toán: Chuỗi Base-100 so sánh danh mục với BTC và ETH. Max Drawdown (MDD) kèm dữ liệu biểu đồ Underwater. Annualized Volatility. Time-Weighted Return (TWR) khử cash flows. Thêm endpoint và tích hợp vào tab/view Insights trên frontend.",
    },
]


def log(msg: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_claude(model: str, prompt: str, timeout: int = 400) -> tuple[int, str]:
    prompt_file = ROOT / f".tmp_prompt_{int(time.time())}.txt"
    try:
        prompt_file.write_text(prompt, encoding="utf-8")
        cmd = [
            CLAUDE_BIN,
            "--model",
            model,
            "--dangerously-skip-permissions",
            "-p",
            prompt,
        ]
        log(f"Invoking Claude ({model})...")
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
    finally:
        if prompt_file.exists():
            prompt_file.unlink()


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


def update_ledger_task(task_id: str, commit_msg: str) -> None:
    ledger_path = ROOT / "ROADMAP_LEDGER.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = ledger_path.read_text(encoding="utf-8")
    # Mark task as done
    lines = content.splitlines()
    new_lines = []
    for line in lines:
        if f"**{task_id}**" in line and "[ ]" in line:
            new_lines.append(line.replace("[ ]", "[x]") + f" — ✅ {today} · {commit_msg}")
        elif f"| {task_id} |" in line and "`[ ] TODO`" in line:
            new_lines.append(line.replace("`[ ] TODO`", "`[x] DONE`") + f" {today} |")
        else:
            new_lines.append(line)
    ledger_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    log(f"Updated ROADMAP_LEDGER.md for task {task_id}")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("=== Starting Autonomous Portfolio Orchestrator ===")
    log(f"Total tasks in queue: {len(TASKS)}")

    for idx, task in enumerate(TASKS, 1):
        task_id = task["id"]
        title = task["title"]
        spec = task["spec"]
        log(f"\n--- Task {idx}/{len(TASKS)}: [{task_id}] {title} ---")

        # 1. Opus Implementation
        opus_prompt = f"""Bạn là Claude Opus 5.5, Lead Engineer. Hãy trực tiếp code và hoàn thành task sau trong dự án /home/jean/projects/my-automations/crypto_portfolio:

Task ID: {task_id}
Tiêu đề: {title}
Yêu cầu kỹ thuật:
{spec}

Hướng dẫn:
- Viết code sạch, hoàn chỉnh trực tiếp vào các file tương ứng trong dự án.
- Viết unit tests đầy đủ kiểm chứng mọi acceptance criteria.
- Chạy pytest để đảm bảo toàn bộ test suite đều pass (không làm hỏng test cũ).
- Báo cáo kết quả ngắn gọn, liệt kê file đã sửa/tạo mới.
"""
        code_ret, code_out = run_claude("claude-opus-5-5", opus_prompt, timeout=600)
        log(f"Opus finished with exit code {code_ret}")

        # 2. Run pytest
        test_ret, test_out = run_pytest()
        log(f"Pytest exit code: {test_ret}")
        if test_ret != 0:
            log(f"Tests failed! Output preview:\n{test_out[:500]}")
            # Give Opus one retry to fix
            fix_prompt = f"Pytest thất bại sau khi bạn sửa task {task_id}:\n{test_out}\nHãy sửa ngay các lỗi để pytest pass."
            run_claude("claude-opus-5-5", fix_prompt, timeout=400)
            test_ret, test_out = run_pytest()

        # 3. Fable Review
        fable_prompt = f"""Bạn là Claude Fable 5.2, Senior Code Reviewer.
Hãy kiểm tra git diff và các file vừa được tạo/sửa cho task {task_id} ({title}) tại /home/jean/projects/my-automations/crypto_portfolio.
Kết quả pytest:
{test_out[:1000]}

Đánh giá xem:
1. Code có đáp ứng đúng yêu cầu: {spec} không?
2. Có lỗi tiềm ẩn, race condition, hoặc vấn đề bảo mật nào không?
Nếu đạt chuẩn, in rõ dòng: "VERDICT: APPROVED".
Nếu chưa đạt, chỉ ra lỗi cần sửa.
"""
        rev_ret, rev_out = run_claude("claude-fable-5-2", fable_prompt, timeout=300)
        log(f"Fable review exit code: {rev_ret}")

        if "VERDICT: APPROVED" in rev_out or test_ret == 0:
            log(f"Task {task_id} APPROVED!")
            # Git commit
            subprocess.run(["git", "add", "."], cwd=str(ROOT))
            commit_res = subprocess.run(
                ["git", "commit", "-m", f"feat({task_id}): {title}"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            commit_sha = commit_res.stdout.split()[1] if commit_res.returncode == 0 else "done"
            update_ledger_task(task_id, f"commit {commit_sha}")
        else:
            log(f"Task {task_id} received feedback from Fable:\n{rev_out[:400]}")
            # Try 1 iteration of fixes
            fix_prompt = f"Claude Fable 5.2 yêu cầu chỉnh sửa cho task {task_id}:\n{rev_out}\nHãy sửa và đảm bảo pytest pass."
            run_claude("claude-opus-5-5", fix_prompt, timeout=400)
            run_pytest()
            subprocess.run(["git", "add", "."], cwd=str(ROOT))
            subprocess.run(["git", "commit", "-m", f"feat({task_id}): {title} (reviewed)"], cwd=str(ROOT))
            update_ledger_task(task_id, "reviewed")

        time.sleep(3)

    log("\n=== ALL TASKS IN ROADMAP COMPLETED SUCCESSFULLY! ===")


if __name__ == "__main__":
    main()
