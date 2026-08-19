# -*- coding: utf-8 -*-
"""报告展示置信度单测（阶段 2 出口收尾工单②，2026-08-20）。

覆盖：词典直判按校准口径封顶 0.6（可信度至多"中"，不再出现 0.98 类高置信），
LLM 置信度原样展示；display_confidence_tier 档位边界（高≥0.8 / 中≥0.5 / 低）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coding.coder import (  # noqa: E402
    LEXICON_DISPLAY_CONFIDENCE_CAP,
    display_confidence,
    display_confidence_tier,
)


def test_lexicon_display_capped_at_06() -> None:
    # 词典直判：高于封顶的置信度一律显示 0.6
    assert LEXICON_DISPLAY_CONFIDENCE_CAP == 0.6
    assert display_confidence(0.98, "lexicon") == 0.6
    assert display_confidence(0.9, "lexicon") == 0.6
    # 低于封顶原样
    assert display_confidence(0.4, "lexicon") == 0.4
    assert display_confidence(0.55, "lexicon") == 0.55
    assert display_confidence(0.6, "lexicon") == 0.6
    print("✓ 词典直判置信度封顶 0.6（高于封顶压平/低于原样）通过")


def test_llm_display_unchanged() -> None:
    assert display_confidence(0.98, "llm") == 0.98
    assert display_confidence(0.4, "llm") == 0.4
    assert display_confidence(0.0, "llm") == 0.0
    print("✓ LLM 置信度原样展示通过")


def test_display_tier_bounds() -> None:
    # 档位基于展示后置信度：高 ≥0.8、中 ≥0.5、低 <0.5
    assert display_confidence_tier(0.92, "llm") == "高"
    assert display_confidence_tier(0.8, "llm") == "高"
    assert display_confidence_tier(0.79, "llm") == "中"
    assert display_confidence_tier(0.5, "llm") == "中"
    assert display_confidence_tier(0.49, "llm") == "低"
    print("✓ 可信度档位边界（高/中/低）通过")


def test_lexicon_never_shows_high() -> None:
    # 词典直判展示封顶 0.6 → 档位至多"中"，即使原始置信度极高
    assert display_confidence_tier(0.98, "lexicon") == "中"
    assert display_confidence_tier(0.99, "lexicon") == "中"
    assert display_confidence_tier(0.45, "lexicon") == "低"
    print("✓ 词典直判可信度至多「中」（校准口径防误导）通过")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_lexicon_display_capped_at_06()
    test_llm_display_unchanged()
    test_display_tier_bounds()
    test_lexicon_never_shows_high()
    print("display_confidence 单测全部通过 ✅")


if __name__ == "__main__":
    main()
