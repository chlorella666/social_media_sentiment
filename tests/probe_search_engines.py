# -*- coding: utf-8 -*-
"""WebSearch 引擎只读探针（2026-08-13）：诊断 360 / cn.bing / 夸克 的真实响应。

只发 1 次请求，输出：状态码、页面长度、风控特征词、用现有解析器能解析出的结果数、
以及 cn.bing 前 10 条标题样例——用于判断"降级页"还是"解析器问题"。

用法：python tests/probe_search_engines.py [--query 小象超市 评价]
"""

from __future__ import annotations

import argparse
import re
import sys
from html import unescape
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUERY_DEFAULT = "\u5c0f\u8c61\u8d85\u5e02 \u8bc4\u4ef7"  # 小象超市 评价
RISK_MARKERS = (
    "验证码", "安全验证", "captcha", "访问过于频繁", "操作频繁",
    "请输入验证码", "滑动验证", "请求过于频繁", "无结果",
)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _strip_tags(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _generic_titles(html_text: str, limit: int = 10) -> list[str]:
    """通用标题提取：<h2>/<h3> 或 <a> 链接文本（跨引擎兜底诊断用）。"""
    out: list[str] = []
    for m in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html_text, flags=re.S):
        t = _strip_tags(m)
        if t and t not in out:
            out.append(t[:60])
        if len(out) >= limit:
            break
    if not out:
        for m in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                            html_text, flags=re.S):
            t = _strip_tags(m[1])
            if t and t not in out:
                out.append(f"{t[:50]} | {m[0][:60]}")
            if len(out) >= limit:
                break
    return out


def probe(query: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from app.channels import websearch as ws

    engines = [
        ("360", "https://www.so.com/s", {"q": query, "pn": 1}),
        ("cn.bing", "https://cn.bing.com/search",
         {"q": query, "setlang": "zh-hans"}),
        ("quark", "https://quark.sm.cn/s", {"q": query}),
    ]
    print(f"探针查询：{query}\n")
    for name, base, params in engines:
        headers = dict(HEADERS)
        headers["User-Agent"] = USER_AGENTS[0]
        if name == "360":
            headers["Referer"] = "https://www.so.com/"
        elif name == "cn.bing":
            headers["Referer"] = "https://cn.bing.com/"
        else:
            headers["Referer"] = "https://quark.sm.cn/"
        try:
            r = requests.get(base, params=params, headers=headers, timeout=25)
            html = r.text
            markers = [m for m in RISK_MARKERS if m in html]
            n_360 = len(ws._parse_360(html)) if name == "360" else 0
            n_bing = len(ws._parse_bing(html)) if name == "cn.bing" else 0
            titles = _generic_titles(html)
            print(f"=== {name} ===")
            print(f"  HTTP {r.status_code} | 页面长度 {len(html)} | "
                  f"现有解析器结果：360={n_360} bing={n_bing}")
            print(f"  风控特征：{markers if markers else '无'}")
            print(f"  通用标题样例（前 {min(len(titles), 10)} 条）：")
            for t in titles[:10]:
                print("   -", t)
            print()
        except Exception as exc:
            print(f"=== {name} === 请求异常：{exc}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="WebSearch 引擎只读探针")
    ap.add_argument("--query", default=QUERY_DEFAULT)
    args = ap.parse_args()
    probe(args.query)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
