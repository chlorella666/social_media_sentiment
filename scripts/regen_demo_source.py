"""重新生成 / 恢复演示报告真实源（恋与深空示例，v3.8）。

背景：演示报告默认加载 data/state/demo_report/demo_source.json（真实任务输出，
本地 gitignored 不入库）；该文件在「一键清除数据」/换机/重装后会丢失，演示报告
回退到内置虚构「云朵咖啡」。本脚本用于：
  1) submit：用已有恋与深空 result.json 的原始计划重新提交一个任务
     （用当前 Prompt v3.8 重新编码，需 API Key + worker 运行）；
  2) sync：把新任务的 result.json 同步为 demo_source.json 并清掉演示缓存，
     下次打开「看演示报告」即用新源。

用法：
  python scripts/regen_demo_source.py submit --from <result.json | 任务目录> [--dry-run]
  python scripts/regen_demo_source.py sync --from <result.json | 任务目录>

说明：
  - submit 需要 LLM Key（应用侧边栏「保存到本机」DPAPI 或环境变量
    OPENAI_API_KEY）且后台 worker 正在运行（run.bat）；提交后到
    「后台任务中心」查看进度，任务完成后再执行 sync。
  - 微博渠道 Cookie 由 worker 从 DPAPI 恢复；无 Cookie 时该渠道降级不影响其余渠道。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import jobs
from app.core.models import AnalysisPlan

DEMO_DIR = ROOT / "data" / "state" / "demo_report"
DEMO_SOURCE = DEMO_DIR / "demo_source.json"
DEMO_CACHE_FILES = ("result.json", "report.html", "result.xlsx")


def _resolve_result(arg: str) -> Path:
    """解析参数：result.json 路径或任务输出目录。"""
    p = Path(arg)
    if p.is_dir():
        rp = p / "result.json"
        if rp.exists():
            return rp
        raise SystemExit(f"未找到 {rp}")
    if not p.exists():
        raise SystemExit(f"未找到 {p}")
    return p


def _load_plan(result_path: Path) -> AnalysisPlan:
    data = json.loads(result_path.read_text(encoding="utf-8"))
    return AnalysisPlan.model_validate(data["plan"])


def _cmd_submit(args) -> int:
    result_path = _resolve_result(args.src)
    plan = _load_plan(result_path)
    print(
        f"计划：{plan.subject} · {plan.domain_id or '不分类'} · "
        f"{len(plan.keywords)} 关键词 · "
        f"{'、'.join(c.channel_id for c in plan.channels)} · "
        f"LLM={'开' if plan.llm_enabled else '关'}"
    )
    if args.dry_run:
        print("[dry-run] 不提交；以上为将复现的原计划。")
        return 0
    task_id = jobs.submit_task(plan)
    print(f"已提交任务：{task_id}")
    print("请在应用「后台任务中心」查看进度；完成后运行：")
    print(f"  python scripts/regen_demo_source.py sync --from <新任务 result.json 或目录>")
    return 0


def _cmd_sync(args) -> int:
    result_path = _resolve_result(args.src)
    data = json.loads(result_path.read_text(encoding="utf-8"))
    plan = data.get("plan") or {}
    subject = plan.get("subject", "")
    if not data.get("coded_items"):
        raise SystemExit(f"{result_path} 没有编码结果（coded_items 为空），拒绝同步。")
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(result_path, DEMO_SOURCE)
    # 清掉演示缓存，下次打开「看演示报告」按新源重建
    removed = []
    for name in DEMO_CACHE_FILES:
        p = DEMO_DIR / name
        if p.exists():
            p.unlink()
            removed.append(name)
    print(
        f"已同步演示源：{DEMO_SOURCE}\n"
        f"  主题：{subject} · 编码 {len(data.get('coded_items') or [])} 条 · "
        f"LLM={'开' if plan.get('llm_enabled') else '关'}\n"
        f"已清除缓存：{', '.join(removed) or '无'}"
    )
    print("打开应用点「✨ 看演示报告」即可查看新示例。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="重新生成/恢复演示报告真实源")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_submit = sub.add_parser("submit", help="用原计划重新提交任务")
    p_submit.add_argument("--from", dest="src", required=True,
                          help="源 result.json 或任务输出目录")
    p_submit.add_argument("--dry-run", action="store_true",
                          help="只打印计划，不提交")
    p_sync = sub.add_parser("sync", help="同步新结果到演示源")
    p_sync.add_argument("--from", dest="src", required=True,
                        help="新任务 result.json 或任务输出目录")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.cmd == "submit":
        return _cmd_submit(args)
    return _cmd_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
