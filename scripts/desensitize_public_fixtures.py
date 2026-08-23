# -*- coding: utf-8 -*-
"""公开仓库前的一次性裁判集脱敏工具（v1：2026-08-20 决策清单 #1；v2：2026-08-23 补齐）。

对 tests/fixtures/*.csv 做 PII 打码：
- text 列：URL/@提及/邮箱/手机号/身份证号 + 裸露用户名（贴吧用户_*、tap 主页/动态所有者）
  → 中性占位符；
- text_id 列（v2 补齐）：链接/BV 号/小红书笔记 ID 等平台标识 → 稳定唯一占位符
  （保留 :post/:comment#N 结构后缀；相同原值映射同一占位符，行 ID 语义不变）。
其他列与行结构原样保留。评测管道在打分前会先 clean_text 剥离上述字段，因此
打码不改变词典得分与黄金集准确率（已验证：脱敏前后 0.5086 一致，回归 38/38）。

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
# v2：裸露用户名（非 @ 形式）
TIEBA_USER_RE = re.compile(r"贴吧用户_[A-Za-z0-9_]+")
RANK_USER_RE = re.compile(r"(?<![\w\u4e00-\u9fff])[\w\u4e00-\u9fff]{1,16}初涉江湖")
TAPTOP_PROFILE_RE = re.compile(r"[\w\u4e00-\u9fff·]{1,16}的个人主页")
TAPTOP_MOMENT_RE = re.compile(r"[\w\u4e00-\u9fff·]{1,16}的动态")
# v2：text_id 中的平台标识
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
XHS_NOTE_RE = re.compile(r"(?<![\w\d])[0-9a-f]{20,}(?![\w\d])")


def mask_text(text: str) -> str:
    text = ID_RE.sub("[身份证号]", text)
    text = PHONE_RE.sub("[手机号]", text)
    text = URL_RE.sub("[链接]", text)
    text = EMAIL_RE.sub("[邮箱]", text)
    text = MENTION_RE.sub("[@用户]", text)
    text = WEIBO_HANDLE_RE.sub("[用户名]", text)
    text = TIEBA_USER_RE.sub("[用户名]", text)
    text = RANK_USER_RE.sub("[用户名]初涉江湖", text)
    text = TAPTOP_PROFILE_RE.sub("[用户名]的个人主页", text)
    text = TAPTOP_MOMENT_RE.sub("[用户名]的动态", text)
    return text


def mask_text_id(value: str, id_map: dict, counter: list[int]) -> str:
    """text_id 打码：链接/BV 号/小红书笔记 ID → 稳定唯一占位符。

    保留 :post/:comment#N 结构后缀；相同原值映射到同一占位符，保证行 ID 唯一性
    （标注/评测脚本以 text_id 为键，见 benchmark_golden.py / edge_annotation.py）。
    """
    if value.startswith("["):
        return value
    m = re.match(r"^(.*?)(:post|:comment(?:#\d+)?)?$", value)
    body, suffix = m.group(1), m.group(2) or ""
    if URL_RE.search(body):
        kind = "链接"
    elif BV_RE.search(body):
        kind = "视频ID"
    elif XHS_NOTE_RE.search(body) or re.fullmatch(r"[0-9a-fA-F]{16,}", body):
        kind = "笔记ID"
    elif not re.fullmatch(r"[\w\u4e00-\u9fff\-]+", body):
        kind = "文本ID"
    else:
        return value
    key = (kind, body)
    if key not in id_map:
        counter[0] += 1
        id_map[key] = f"[{kind}{counter[0]}]"
    return id_map[key] + suffix


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
        id_idx = header.index("text_id") if "text_id" in header else None
        text_idx = header.index("text") if "text" in header else None
        if id_idx is None and text_idx is None:
            continue
        changed_rows = 0
        id_map: dict = {}
        counter = [0]
        for row in rows[1:]:
            row_changed = False
            if id_idx is not None and id_idx < len(row):
                new_id = mask_text_id(row[id_idx], id_map, counter)
                if new_id != row[id_idx]:
                    row[id_idx] = new_id
                    row_changed = True
            if text_idx is not None and text_idx < len(row):
                new_text = mask_text(row[text_idx])
                if new_text != row[text_idx]:
                    row[text_idx] = new_text
                    row_changed = True
            if row_changed:
                changed_rows += 1
        if changed_rows == 0:
            continue
        with open(path, "w", newline="", encoding="utf-8") as f:
            if has_bom:
                f.write("\ufeff")
            writer = csv.writer(f, lineterminator=newline)
            writer.writerows(rows)
        total_changed += 1
        print(f"✓ {path.name}: {changed_rows} 行 text/text_id 已打码")
    print(f"完成：共处理 {total_changed} 个 CSV" if total_changed else "完成：无命中，全部文件未改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())