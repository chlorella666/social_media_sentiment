"""全局视觉主题注入（P0-2/P1）。

- 样式单一来源：app/ui/global.css（源自 design_entries/ui-designer/streamlit_global.css，
  选择器已按当前 Streamlit 版本核对）；
- 主应用在 st.set_page_config 之后调用一次 inject_global_css()；
- 只做视觉层，不改业务逻辑。
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

_CSS_PATH = Path(__file__).resolve().parent / "global.css"


def inject_global_css() -> None:
    """把全局 CSS 注入当前页面（幂等；文件缺失时静默跳过，不阻塞应用）。

    注意：必须用 st.markdown(unsafe_allow_html=True) 且 CSS 压缩为单行——
    Streamlit 1.60 的 markdown 管线会把含空行的 <style> 块拆分为可见文本
    （实测 body 中出现整段 CSS 源码）；单行 style 块可正常作为样式生效。
    st.html 也不可用：其 DOMPurify 清洗会剥离 <style> 标签。
    """
    try:
        css = _CSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    # 压缩空白为单行（CSS 语义等价：多余空白/换行无意义），避免 markdown 块被拆
    css_one_line = " ".join(css.split())
    st.markdown(f"<style>{css_one_line}</style>", unsafe_allow_html=True)
