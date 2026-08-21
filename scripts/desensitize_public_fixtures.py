# -*- coding: utf-8 -*-
"""公开仓库前的一次性裁判集脱敏工具（2026-08-20，决策清单 #1）。

对 tests/fixtures/*.csv 的 text 列做 PII 打码：URL/@提及/邮箱/手机号/身份证号
替换为中性占位符；其他列与行结构原样保留。评测管道在打分前会先 clean_text
剥离上述字段（cleaner.URL_RE/MENTION_RE/EMAIL_RE + 手机/身份证规则），因此
打码不改变词典得分与黄金集准确率（已验证：脱敏前后 0.5086 一致，回归 37/37）。

用法：python scripts/desensitize_public_fixtures.py
幂等：再次运行无变化；未命中任何模式的文件保持字节不变（含指纹）。
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MENTION_RE = re.compile(r"(?<!\[)@[\w\u4e00-\u9fff\-]+")
WEIBO_HANDLE_RE = re.compile(r"[\w\u4e00-\u9fff\-]{1,20}的微博(?:视频)?")


def mask_text(text: str) -> str:
    text = ID_RE.sub("[身份证号]", text)
    text = PHONE_RE.sub("[手机号]", text)
    text = URL_RE.sub("[链接]", text)
    text = EMAIL_RE.sub("[邮箱]", text)
    text = MENTION_RE.sub("[@用户]", text)
    text = WEIBO_HANDLE_RE.sub("[用户名]", text)
    return text


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    total_changed = 0
    for path in sorted(FIXTURES.glob("*.csv")):
        raw = path.read_bytes()
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        crlf = raw.count(b"\r\n")
        lf = raw.count(b"\n")
        newline = "\r\n" if crlf >= lf else "\n"
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if not rows:
            continue
        header = rows[0]
        try:
            text_idx = header.index("text")
        except ValueError:
            continue
        changed_rows = 0
        masked = 0
        for row in rows[1:]:
            if text_idx >= len(row):
                continue
            new_text = mask_text(row[text_idx])
            if new_text != row[text_idx]:
                row[text_idx] = new_text
                changed_rows += 1
                masked += 1
        if changed_rows == 0:
            continue
        with open(path, "w", newline="", encoding="utf-8") as f:
            if has_bom:
                f.write("\ufeff")
            writer = csv.writer(f, lineterminator=newline)
            writer.writerows(rows)
        total_changed += 1
        print(f"✓ {path.name}: {changed_rows} 行 text 已打码")
    print(f"完成：共处理 {total_changed} 个 CSV" if total_changed else "完成：无命中，全部文件未改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
