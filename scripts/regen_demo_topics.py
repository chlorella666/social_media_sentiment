# -*- coding: utf-8 -*-
"""F-020（2026-08-26）：demo_source 快照重建——LLM 编码工作流生成恋与深空 topics。"""
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
from app.coding.coding_workflow import run_coding_workflow

bundle = ReportBundle.model_validate(data)
key = load_api_key(allow_env=False)
if not key:
    print("SKIP: 未配置 API Key")
    sys.exit(2)

analyzer = create_analyzer(
    api_key=key, base_url="https://api.deepseek.com", model="deepseek-chat"
)
ok, msg = analyzer.ping()
print("LLM ping:", ok, msg)
if not ok:
    sys.exit(2)

topics = run_coding_workflow(analyzer, bundle.coded_items, bundle.plan)
print("topics:", len(topics))
for t in topics[:10]:
    print(" -", t["name"], "| type:", t.get("type"), "| pol:", t.get("polarity"), "| n:", t.get("count"), "| attribution:", t.get("attribution"), "| insight:", (t.get("insight") or "")[:30])

if topics:
    data["summary"]["topics"] = topics
    _pos_t = [t for t in topics if t.get("polarity") == "positive"][:40]
    _neg_t = [t for t in topics if t.get("polarity") == "negative"][:40]
    data["summary"]["positive_wordcloud"] = [(t["name"].replace(" ", "\u3000"), t["count"]) for t in _pos_t]
    data["summary"]["negative_wordcloud"] = [(t["name"].replace(" ", "\u3000"), t["count"]) for t in _neg_t]
    data["summary"]["worst_dim_wordcloud"] = data["summary"]["negative_wordcloud"]
    with io.open(DST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("SAVED topics to", DST)
else:
    print("WARN: 工作流未产出 topics（保留规则兜底）")