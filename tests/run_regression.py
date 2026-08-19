"""一键回归（1.6）：单元测试 + 黄金集门槛 + 可选真实验收。

跨平台：仅使用标准库（subprocess/pathlib/json），Windows 与 POSIX 均可运行；
CI 双平台矩阵（windows-latest + ubuntu-latest）验证此假设。

用法：
    python tests/run_regression.py                    # 默认：全部单元测试 + 词典评测门槛
    python tests/run_regression.py --unit-only        # 只跑单元测试
    python tests/run_regression.py --benchmark-only   # 只跑黄金集评测（含门槛）
    python tests/run_regression.py --llm              # 追加混合评测（需 API Key，仅记录不设门槛）
    python tests/run_regression.py --acceptance       # 追加真实验收（需微博 Cookie，不进 CI）
    python tests/run_regression.py --accept-baseline  # 显式接受当前词典准确率为新基线
    python tests/run_regression.py --record           # 追加结果到 docs/评测记录.md（默认不写）
    python tests/run_regression.py --ci               # CI 模式（不写评测记录 + 禁验收/禁 LLM）
    python tests/run_regression.py --smoke            # runner 自检（跨平台契约检查）

退出码：全部通过 0；任一单元测试失败 / 词典基线跌破门槛 / 验收失败 → 1。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REGRESSION_VERSION = "1.0"

# 单元测试白名单（显式列出，避免误收录工具脚本/递归自检）
UNIT_TESTS = [
    "test_ad_rules.py",
    "test_cleaner.py",
    "test_channel_strategy.py",
    "test_composer.py",
    "test_collection_transparency.py",
    "test_consumer_voice.py",
    "test_contracts.py",
    "test_custom_dimension.py",
    "test_dimension_aspect.py",
    "test_domain_proposer.py",
    "test_edge_annotation.py",
    "test_errors.py",
    "test_eval_dashboard.py",
    "test_eval_store.py",
    "test_evidence.py",
    "test_health.py",
    "test_edge_sampling.py",
    "test_job_queue.py",
    "test_keyword_effects.py",
    "test_lifecycle.py",
    "test_lexicon_v2.py",
    "test_llm_analyzer.py",
    "test_need_review.py",
    "test_pipeline_progress.py",
    "test_quota.py",
    "test_regression_runner.py",
    "test_report_insights.py",
    "test_review.py",
    "test_secrets.py",
    "test_tokenizer.py",
    "test_ui_flow.py",
    "test_usage_boundary.py",
    "test_validation_discipline.py",
    "test_websearch_robust.py",
]

BENCHMARK_SCRIPT = ROOT / "tests" / "benchmark_golden.py"
ACCEPTANCE_SCRIPT = ROOT / "tests" / "manual_acceptance.py"
BASELINE_FIXTURE = ROOT / "tests" / "fixtures" / "baseline_lexicon.json"
EDGE_BENCHMARK_JSON = ROOT / "data" / "datasets" / "edge_benchmark_report.json"
HISTORY_FILE = ROOT / "data" / "regression" / "history.jsonl"
DEFAULT_REPORT_DIR = ROOT / "data" / "regression"
DEFAULT_TOLERANCE_PP = 1.0
DEFAULT_TEST_TIMEOUT_S = 600
DEFAULT_ACCEPTANCE_TIMEOUT_S = 1800


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _commit_short() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _run(cmd: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """跨平台子进程执行：UTF-8 捕获，超时即杀。"""
    return subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _tail(text: str, lines: int = 30) -> str:
    parts = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(parts[-lines:]) if parts else ""


def run_unit_test(
    path: Path,
    timeout: int,
    allow_retry: bool,
) -> dict:
    """单个测试文件子进程执行；失败可重跑 1 次（降 flaky 假红）。"""
    start = time.monotonic()
    result = {
        "file": path.name,
        "status": "FAIL",
        "exit_code": -1,
        "duration_s": 0.0,
        "retried": False,
        "output_tail": "",
    }
    try:
        proc = _run([sys.executable, str(path)], timeout=timeout)
        duration = round(time.monotonic() - start, 2)
        if proc.returncode == 0:
            result.update(status="PASS", exit_code=0, duration_s=duration,
                          output_tail=_tail(proc.stdout + proc.stderr))
            return result
        if allow_retry:
            result["retried"] = True
            start = time.monotonic()
            proc = _run([sys.executable, str(path)], timeout=timeout)
            duration = round(time.monotonic() - start, 2)
        result.update(exit_code=proc.returncode, duration_s=duration,
                      output_tail=_tail(proc.stdout + proc.stderr))
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - start, 2)
        out = (exc.stdout or "") + (exc.stderr or "")
        result.update(status="TIMEOUT", exit_code=124, duration_s=duration,
                      output_tail=_tail(str(out)))
    except Exception as exc:  # runner 自身异常视为该测试失败
        result.update(output_tail=f"runner 异常：{exc}")
    return result


def run_benchmark(llm: bool, record: bool, report_out: Path | None) -> dict:
    """跑黄金集评测，返回报告摘要。CI 路径传 --no-record。"""
    args = [sys.executable, str(BENCHMARK_SCRIPT)]
    if llm:
        args.append("--llm")
    if not record:
        args.append("--no-record")
    if report_out is not None:
        args += ["--report-out", str(report_out)]
    start = time.monotonic()
    proc = _run(args, timeout=900)
    duration = round(time.monotonic() - start, 2)
    report = {}
    target = report_out or (ROOT / "data" / "datasets" / "benchmark_report.json")
    if proc.returncode == 0 and target.exists():
        try:
            report = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    return {
        "mode": "hybrid" if llm else "lexicon",
        "exit_code": proc.returncode,
        "duration_s": duration,
        "accuracy": report.get("sentiment_accuracy"),
        "by_domain": report.get("by_domain"),
        "output_tail": _tail(proc.stdout + proc.stderr),
        "report_path": str(target),
    }


def run_edge_benchmark(record: bool, report_out: Path | None) -> dict:
    """跑边界集词典评测（strict-edge A 档：只提示 + 人工验收，不设硬门槛）。"""
    args = [sys.executable, str(BENCHMARK_SCRIPT), "--edge"]
    if not record:
        args.append("--no-record")
    if report_out is not None:
        args += ["--report-out", str(report_out)]
    start = time.monotonic()
    proc = _run(args, timeout=900)
    duration = round(time.monotonic() - start, 2)
    report = {}
    target = report_out or EDGE_BENCHMARK_JSON
    if proc.returncode == 0 and target.exists():
        try:
            report = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
    return {
        "mode": "edge_lexicon",
        "exit_code": proc.returncode,
        "duration_s": duration,
        "accuracy": report.get("sentiment_accuracy"),
        "by_subset": report.get("by_subset"),
        "calibration": report.get("calibration"),
        "output_tail": _tail(proc.stdout + proc.stderr),
        "report_path": str(target),
    }


def load_baseline() -> dict | None:
    if not BASELINE_FIXTURE.exists():
        return None
    try:
        return json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_baseline(data: dict) -> None:
    BASELINE_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FIXTURE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def append_history(entry: dict) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 历史记录 best-effort，不阻塞回归


def gate_check(result: dict, tolerance_pp: float, accept_baseline: bool) -> dict:
    """词典直判门槛：accuracy >= 基线 - tolerance。跌破需 --accept-baseline 显式更新。"""
    mode = result.get("mode", "lexicon")
    accuracy = result.get("accuracy")
    if mode != "lexicon":
        return {"gate": "SKIP", "reason": "混合评测仅记录，不设门槛"}
    if accuracy is None:
        return {"gate": "FAIL", "reason": f"词典评测失败（exit={result.get('exit_code')}），未产出准确率"}
    baseline = load_baseline()
    if baseline is None:
        base = {
            "mode": "lexicon",
            "accuracy": round(accuracy, 4),
            "n": result.get("n"),
            "golden": "tests/fixtures/golden_set_v1.csv",
            "tolerance_pp": tolerance_pp,
            "created_at": _iso(),
            "commit": _commit_short(),
            "note": "首次回归自动建立基线",
        }
        save_baseline(base)
        append_history({"ts": _iso(), "commit": base["commit"], "mode": mode,
                        "accuracy": base["accuracy"], "action": "baseline_created"})
        return {"gate": "PASS", "baseline": base["accuracy"], "tolerance_pp": tolerance_pp,
                "note": "无历史基线，已以当前值为基线（首次）"}
    base_acc = baseline.get("accuracy")
    if base_acc is None:
        return {"gate": "FAIL", "reason": "基线文件缺失 accuracy 字段"}
    floor = base_acc - tolerance_pp / 100.0
    passed = accuracy >= floor - 1e-9
    out = {"gate": "PASS" if passed else "FAIL", "baseline": base_acc,
           "tolerance_pp": tolerance_pp, "floor": round(floor, 4),
           "accuracy": round(accuracy, 4)}
    if not passed and accept_baseline:
        new_base = {
            "mode": "lexicon",
            "accuracy": round(accuracy, 4),
            "n": result.get("n"),
            "golden": "tests/fixtures/golden_set_v1.csv",
            "tolerance_pp": tolerance_pp,
            "updated_at": _iso(),
            "commit": _commit_short(),
            "note": f"由 --accept-baseline 显式接受（旧基线 {base_acc}）",
        }
        save_baseline(new_base)
        append_history({"ts": _iso(), "commit": new_base["commit"], "mode": mode,
                        "accuracy": new_base["accuracy"], "action": "baseline_accepted",
                        "old_accuracy": base_acc})
        out.update(gate="PASS", baseline=new_base["accuracy"],
                   note="已通过 --accept-baseline 接受新基线（需提交基线文件）")
    return out


def run_acceptance(timeout: int) -> dict:
    """真实验收（manual_acceptance.py）：需微博 Cookie，否则直接 BLOCKED。"""
    if not ACCEPTANCE_SCRIPT.exists():
        return {"status": "BLOCKED", "reason": f"未找到 {ACCEPTANCE_SCRIPT.name}"}
    if not os.environ.get("WB_SUB"):
        return {"status": "BLOCKED", "reason": "缺少 WB_SUB（微博 Cookie），真实验收不进 CI 且需人工前置"}
    start = time.monotonic()
    proc = _run([sys.executable, str(ACCEPTANCE_SCRIPT)], timeout=timeout)
    return {
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "exit_code": proc.returncode,
        "duration_s": round(time.monotonic() - start, 2),
        "output_tail": _tail(proc.stdout + proc.stderr, 60),
    }


def run_smoke() -> int:
    """runner 自检：用临时 pass/fail 测试文件验证子进程/重试/报告/退出码契约。"""
    tmp = Path(tempfile.mkdtemp(prefix="sms_regress_smoke_"))
    (tmp / "test_smoke_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n\n"
        "if __name__ == '__main__':\n    test_ok()\n    print('smoke ok')\n",
        encoding="utf-8",
    )
    (tmp / "test_smoke_fail.py").write_text(
        "def test_fail() -> None:\n    assert False\n\n"
        "if __name__ == '__main__':\n    test_fail()\n",
        encoding="utf-8",
    )
    ok = run_unit_test(tmp / "test_smoke_ok.py", timeout=120, allow_retry=False)
    fail = run_unit_test(tmp / "test_smoke_fail.py", timeout=120, allow_retry=False)
    report = {
        "tool": "run_regression.py",
        "regression_version": REGRESSION_VERSION,
        "smoke": True,
        "platform": platform.platform(),
        "results": [ok, fail],
        "summary": {
            "passed": 1 if ok["status"] == "PASS" else 0,
            "failed": 1 if fail["status"] != "PASS" else 0,
        },
    }
    report_path = tmp / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    good = ok["status"] == "PASS" and fail["status"] != "PASS"
    print(f"SMOKE_{'OK' if good else 'FAIL'} {report_path}")
    return 0 if good else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="一键回归：单元测试 + 黄金集门槛 + 可选验收")
    ap.add_argument("--unit-only", action="store_true")
    ap.add_argument("--benchmark-only", action="store_true")
    ap.add_argument("--llm", action="store_true", help="追加混合评测（仅记录，不设门槛）")
    ap.add_argument("--acceptance", action="store_true", help="追加真实验收（需 WB_SUB，不进 CI）")
    ap.add_argument("--accept-baseline", action="store_true", help="显式接受当前词典基线")
    ap.add_argument("--record", action="store_true", help="追加结果到 docs/评测记录.md（默认不写）")
    ap.add_argument("--ci", action="store_true", help="CI 模式：--no-record + 禁验收/禁 LLM")
    ap.add_argument("--smoke", action="store_true", help="runner 自检后退出")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--no-retry", action="store_true", help="失败不自动重跑")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TEST_TIMEOUT_S)
    ap.add_argument("--tolerance-pp", type=float, default=DEFAULT_TOLERANCE_PP)
    ap.add_argument("--report-dir", type=Path, default=None)
    args = ap.parse_args()

    if args.smoke:
        return run_smoke()
    if args.ci and (args.acceptance or args.llm):
        print("CI 模式禁止 --acceptance / --llm（凭据与费用不进 CI）")
        return 2
    record = args.record and not args.ci

    report_dir = args.report_dir or (DEFAULT_REPORT_DIR / _ts())
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "tool": "run_regression.py",
        "regression_version": REGRESSION_VERSION,
        "started_at": _iso(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "commit": _commit_short(),
        "args": vars(args),
        "unit_tests": [],
        "benchmark": None,
        "acceptance": None,
        "summary": {},
    }
    failed_units: list[str] = []

    if not args.benchmark_only:
        for name in UNIT_TESTS:
            path = ROOT / "tests" / name
            if not path.exists():
                failed_units.append(name)
                report["unit_tests"].append(
                    {"file": name, "status": "MISSING", "exit_code": -1,
                     "duration_s": 0.0, "retried": False, "output_tail": "文件不存在"}
                )
                if args.fail_fast:
                    break
                continue
            res = run_unit_test(path, timeout=args.timeout, allow_retry=not args.no_retry)
            report["unit_tests"].append(res)
            print(f"{'✅' if res['status'] == 'PASS' else '❌'} {name} "
                  f"({res['status']}, {res['duration_s']}s"
                  + (", 重跑后通过" if res.get("retried") and res["status"] == "PASS" else "")
                  + ")")
            if res["status"] != "PASS":
                failed_units.append(name)
                if args.fail_fast:
                    break

    gate_pass = True
    if not args.unit_only:
        bench_report_out = report_dir / "benchmark_lexicon.json"
        bench = run_benchmark(llm=False, record=record, report_out=bench_report_out)
        gate = gate_check(bench, tolerance_pp=args.tolerance_pp,
                          accept_baseline=args.accept_baseline)
        bench["gate"] = gate
        report["benchmark"] = bench
        acc = bench.get("accuracy")
        print(f"黄金集（词典直判）：准确率 {acc:.1%}" if acc is not None
              else f"黄金集（词典直判）：评测失败（exit={bench.get('exit_code')}）")
        print(f"  门槛：{gate}")
        gate_pass = gate.get("gate") == "PASS"
        if args.llm:
            hybrid_out = report_dir / "benchmark_hybrid.json"
            hybrid = run_benchmark(llm=True, record=record, report_out=hybrid_out)
            hybrid["gate"] = {"gate": "SKIP", "reason": "混合评测仅记录，不设门槛"}
            report["benchmark_hybrid"] = hybrid
            hacc = hybrid.get("accuracy")
            print(f"黄金集（混合 LLM）：准确率 {hacc:.1%}" if hacc is not None
                  else f"黄金集（混合 LLM）：评测失败（exit={hybrid.get('exit_code')}）")
            append_history({"ts": _iso(), "commit": report["commit"], "mode": "hybrid",
                            "accuracy": hacc, "action": "record"})
        # 边界集词典评测：strict-edge A 档，只提示不设硬门槛（提示词迭代的裁判基线）
        edge_out = report_dir / "benchmark_edge_lexicon.json"
        edge = run_edge_benchmark(record=record, report_out=edge_out)
        edge["gate"] = {"gate": "PROMPT",
                        "reason": "边界集 strict-edge A 档：只提示 + 人工验收，不设硬门槛"}
        report["benchmark_edge"] = edge
        eacc = edge.get("accuracy")
        if eacc is not None:
            print(f"边界集（词典直判）：准确率 {eacc:.1%}（仅提示，不设门槛）")
        else:
            print(f"边界集（词典直判）：评测失败（exit={edge.get('exit_code')}）")

    acceptance_status = "SKIP"
    if args.acceptance and not args.ci:
        acc_res = run_acceptance(DEFAULT_ACCEPTANCE_TIMEOUT_S)
        report["acceptance"] = acc_res
        acceptance_status = acc_res["status"]
        print(f"真实验收：{acceptance_status}（{acc_res.get('reason', '')}）")
        if acceptance_status == "FAIL":
            print("  验收输出尾部：\n" + acc_res.get("output_tail", ""))

    ok = (not failed_units) and gate_pass and acceptance_status in ("SKIP", "PASS", "BLOCKED")
    report["summary"] = {
        "unit_total": len(report["unit_tests"]),
        "unit_passed": sum(1 for r in report["unit_tests"] if r["status"] == "PASS"),
        "unit_failed": len(failed_units),
        "gate": "PASS" if gate_pass else "FAIL",
        "acceptance": acceptance_status,
        "result": "PASS" if ok else "FAIL",
    }
    report["finished_at"] = _iso()
    report_path = report_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 回归汇总 ===")
    print(f"单元测试：{report['summary']['unit_passed']}/{report['summary']['unit_total']} 通过")
    if failed_units:
        print("失败清单：" + "、".join(failed_units))
    print(f"黄金集门槛：{report['summary']['gate']}")
    print(f"真实验收：{acceptance_status}")
    print(f"报告：{report_path}")
    print("总体：PASS ✅" if ok else "总体：FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
