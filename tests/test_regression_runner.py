"""一键回归 runner 契约测试（1.6）：跨平台自检 + 报告结构。

运行：python tests/test_regression_runner.py
通过子进程调用 run_regression.py --smoke：验证子进程隔离、失败判定、
报告 JSON 结构与退出码在任意平台（Windows/POSIX）行为一致。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_smoke_contract() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "run_regression.py"), "--smoke"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, f"smoke 应退出 0：\n{proc.stdout}\n{proc.stderr}"
    marker = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("SMOKE_OK")),
        None,
    )
    assert marker, f"未输出 SMOKE_OK：\n{proc.stdout}\n{proc.stderr}"
    report_path = Path(marker.split(" ", 1)[1].strip())
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["smoke"] is True
    results = {r["file"]: r for r in report["results"]}
    assert results["test_smoke_ok.py"]["status"] == "PASS"
    assert results["test_smoke_fail.py"]["status"] != "PASS"
    assert report["summary"]["passed"] == 1 and report["summary"]["failed"] == 1
    assert report["platform"], "报告应记录平台信息"
    print("✓ runner --smoke：子进程/失败判定/报告结构/退出码 通过")


def test_gate_logic() -> None:
    """黄金集门槛逻辑：跌破 FAIL、容差内 PASS、--accept-baseline 显式更新。"""
    import run_regression

    tmp = Path(tempfile.mkdtemp(prefix="sms_gate_test_"))
    fake_baseline = tmp / "baseline.json"
    original = run_regression.BASELINE_FIXTURE
    run_regression.BASELINE_FIXTURE = fake_baseline
    try:
        fake_baseline.write_text(
            json.dumps({"mode": "lexicon", "accuracy": 0.3543, "tolerance_pp": 1.0}),
            encoding="utf-8",
        )
        below = run_regression.gate_check(
            {"mode": "lexicon", "accuracy": 0.3400}, tolerance_pp=1.0, accept_baseline=False
        )
        assert below["gate"] == "FAIL", below
        within = run_regression.gate_check(
            {"mode": "lexicon", "accuracy": 0.3480}, tolerance_pp=1.0, accept_baseline=False
        )
        assert within["gate"] == "PASS", within
        accepted = run_regression.gate_check(
            {"mode": "lexicon", "accuracy": 0.3100}, tolerance_pp=1.0, accept_baseline=True
        )
        assert accepted["gate"] == "PASS", accepted
        updated = json.loads(fake_baseline.read_text(encoding="utf-8"))
        assert updated["accuracy"] == 0.31, "--accept-baseline 应更新基线"
        skipped = run_regression.gate_check(
            {"mode": "hybrid", "accuracy": 0.78}, tolerance_pp=1.0, accept_baseline=False
        )
        assert skipped["gate"] == "SKIP", "混合评测不设门槛"
        broken = run_regression.gate_check(
            {"mode": "lexicon", "accuracy": None, "exit_code": 1},
            tolerance_pp=1.0, accept_baseline=False,
        )
        assert broken["gate"] == "FAIL", "词典评测失败必须 FAIL"
    finally:
        run_regression.BASELINE_FIXTURE = original
    print("✓ 黄金集门槛：跌破 FAIL / 容差内 PASS / 显式接受 / 评测失败 FAIL 通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_smoke_contract()
    test_gate_logic()
    print("一键回归 runner 契约测试通过 ✅")


if __name__ == "__main__":
    main()
