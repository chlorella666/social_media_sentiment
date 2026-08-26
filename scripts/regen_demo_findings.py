# -*- coding: utf-8 -*-
"""F-027（2026-08-27）：demo_source 快照重建——LLM 新提示词生成 conclusion_text + 结构化 findings。"""
import io, json, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

ROOT = Path(r"D:\app\codex\social_media_sentence")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DST = ROOT / "app" / "demo_source.json"
data = json.load(io.open(DST, "r", encoding="utf-8"))

from app.core.models import ReportBundle
from app.core.secrets import load_api_key
from app.coding.llm_analyzer import create_analyzer
from app.coding.insights import build_report_content

bundle = ReportBundle.model_validate(data)
key = load_api_key(allow_env=False)
analyzer = create_analyzer(api_key=key, base_url="https://api.deepseek.com", model="deepseek-chat")
ok, msg = analyzer.ping()
print("ping:", ok, msg)
if not ok:
    sys.exit(2)

rc = build_report_content(analyzer, bundle.plan, bundle.summary, bundle.evidence)
print("insight_mode:", rc.get("insight_mode"))
print("conclusion_text:", (rc.get("conclusion_text") or "")[:80])
print("findings:", len(rc.get("findings") or []))
for f in (rc.get("findings") or [])[:4]:
    print(" -", f.get("id"), "| scope:", f.get("scope"), "| claim:", (f.get("claim") or "")[:40], "| detail:", bool(f.get("detail")), "| action:", bool(f.get("action")))

# 更新 demo_source：findings/conclusion/conclusion_text/chart_insights 用新 LLM 结果
if rc.get("findings"):
    data["findings"] = rc["findings"]
    data["conclusion"] = rc.get("conclusion", data.get("conclusion", ""))
    data["conclusion_text"] = rc.get("conclusion_text", "")
    data["chart_insights"] = rc.get("chart_insights") or data.get("chart_insights", {})
    # topics 保持（LLM 编码工作流已固化，不重跑）
    with io.open(DST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("SAVED demo_source（conclusion_text + 结构化 findings）")
else:
    print("WARN: LLM 未产出 findings，保留原快照")