"""离线重建报告脚本（报告证据链优化方案 · 验收工具）。

从已落盘的 result.json 重建报告阶段（不重新采集），用于：
1) WebSearch 等渠道被风控时仍可验收报告证据链（无需联网采集）；
2) 历史任务升级后一键重跑报告。

用法：
  python scripts/regen_report.py <任务ID | result.json 路径 | 结果目录>
      [--out DIR]        # 输出目录（默认与源 result.json 同目录，会覆盖 report.html）
      [--llm | --no-llm] # 强制 LLM / 词典模式；默认跟随 result.json 的 plan.llm_enabled
      [--recode]         # 复用已有采集数据（posts/评论）重跑编码+报告（不重新采集；
                         # Prompt 升级后重新编码用；需 LLM Key，否则按词典模式编码）

LLM 模式需要凭据：应用链路从 DPAPI 读取（本脚本为开发/验收用，
支持环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding.insights import build_report_content
from app.coding.llm_analyzer import MockAnalyzer, create_analyzer
from app.core.evidence import build_evidence
from app.core.jobs import get_task
from app.core.models import ReportBundle
from app.core.names import register_custom_dim_names
from app.core.pipeline import TaskRunner, bundle_to_json, generate_report_text
from app.core.pricing import cost_from_usage
from app.output.html_report import build_html
from app.output.word_report import build_word

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _resolve_result_path(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        rp = p / "result.json"
        if rp.exists():
            return rp
        raise SystemExit(f"未找到 {rp}")
    if p.suffix == ".json":
        if p.exists():
            return p
        raise SystemExit(f"未找到 {p}")
    # 按任务 ID 查任务队列
    task = get_task(arg)
    if task and task.get("output_dir"):
        rp = Path(task["output_dir"]) / "result.json"
        if rp.exists():
            return rp
    raise SystemExit(f"无法解析任务/路径: {arg}")


def main() -> int:
    ap = argparse.ArgumentParser(description="离线重建报告（不重新采集）")
    ap.add_argument("target", help="任务ID / result.json 路径 / 结果目录")
    ap.add_argument("--out", default=None, help="输出目录（默认与源 result.json 同目录）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--llm", action="store_true", help="强制 LLM 模式")
    mode.add_argument("--no-llm", action="store_true", help="强制词典模式")
    ap.add_argument(
        "--recode", action="store_true",
        help="复用已有采集数据重跑编码+报告（不重新采集；Prompt 升级后重新编码用）",
    )
    args = ap.parse_args()

    result_path = _resolve_result_path(args.target)
    data = json.loads(result_path.read_text(encoding="utf-8"))
    bundle = ReportBundle.model_validate(data)
    plan = bundle.plan
    out_dir = Path(args.out) if args.out else result_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    register_custom_dim_names(plan)
    llm_enabled = plan.llm_enabled
    if args.llm:
        llm_enabled = True
    if args.no_llm:
        llm_enabled = False

    analyzer = (
        create_analyzer(allow_env=True)
        if llm_enabled
        else MockAnalyzer()
    )
    if isinstance(analyzer, MockAnalyzer) and llm_enabled:
        print("⚠ 未提供 OPENAI_API_KEY，将按词典模式重建（可用 --llm + 环境变量强制）")

    if args.recode:
        # 复用已有采集数据（channel_results 中的帖子+评论）重跑编码+报告。
        # 不重新采集；Prompt 升级后用当前版本重新编码（v3.8 等）。
        if not bundle.channel_results:
            raise SystemExit("源 result.json 没有 channel_results（无采集数据），无法 --recode")
        if isinstance(analyzer, MockAnalyzer) and plan.llm_enabled:
            print(
                "⚠ 未提供 OPENAI_API_KEY：将按词典模式编码（不是 LLM 效果）。"
                "如需 v3.8 LLM 重编码，请设置 OPENAI_API_KEY / OPENAI_BASE_URL / "
                "OPENAI_MODEL 后重跑。"
            )
        posts = [p for ch in bundle.channel_results for p in (ch.posts or [])]
        runner = TaskRunner(plan)
        new_bundle = runner.code_and_report(
            posts, bundle.channel_results, list(bundle.warnings or []),
            analyzer=analyzer,
        )
        insight_mode = new_bundle.insight_mode
        evidence = new_bundle.evidence
        findings = new_bundle.findings
        print(f"✓ 已重编码：{len(new_bundle.coded_items)} 条（复用已有采集数据，未重新采集）")
    else:
        summary = bundle.summary or {}
        evidence = build_evidence(
            bundle.coded_items, summary, exclude_ad=bool(plan.exclude_ad_enabled)
        )
        content = build_report_content(analyzer, plan, summary, evidence)
        report_text = generate_report_text(plan, summary, content["findings"])

        new_bundle = bundle.model_copy(
            update={
                "report_text": report_text,
                "chart_insights": content["chart_insights"],
                "conclusion": content["conclusion"],
                "findings": content["findings"],
                "evidence": evidence,
                "insight_mode": content["insight_mode"],
            }
        )
        insight_mode = content["insight_mode"]
        findings = content["findings"]
    if hasattr(analyzer, "usage") and isinstance(analyzer.usage, dict) and analyzer.usage:
        u = dict(analyzer.usage)
        u["estimated_cost"] = cost_from_usage(
            int(u.get("prompt_tokens") or 0),
            int(u.get("completion_tokens") or 0),
        )
        new_bundle.llm_usage = u

    bundle_to_json(new_bundle, out_dir / "result.json")
    (out_dir / "report.html").write_text(
        build_html(new_bundle), encoding="utf-8"
    )
    (out_dir / "report.docx").write_bytes(build_word(new_bundle).getvalue())

    print(f"✓ 已重建：{out_dir}")
    print(f"  insight_mode = {insight_mode}")
    print(f"  findings = {len(findings)} 条，evidence = {len(evidence)} 张")
    for f in findings:
        print(f"  - {f.get('id')} {f.get('claim')}")
    if getattr(analyzer, "errors", None):
        print("  分析器提示：")
        for e in analyzer.errors[:5]:
            print(f"    - {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
