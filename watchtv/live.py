import os
import re
import requests

# ========== 填写源的地址 ==========
URL_LIST = [
    "https://raw.githubusercontent.com/bang359/dsj/refs/heads/main/dsjcs1.txt",
    "https://raw.githubusercontent.com/bj123sd/hycg/refs/heads/main/tv.txt",
]

# ========== 分组映射：左边是源里的分组名，右边是输出时改后的分组名 ==========
GROUP_MAP = {
    "🇭🇰香港": "HS港澳台直播频道",
    "🇹🇼台湾": "HS港澳台直播频道",
    "香港": "HS港澳台直播频道",
    "台湾": "HS港澳台直播频道",
}

# ========== 屏蔽频道列表 ==========（需要逗号隔开，最后不需要）
BLOCK_CHANNELS = {"美勇电视台"}


def parse_any(text: str):
    res = []
    extinf_line = None
    current_group = None
    for raw_line in text.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        if ln.startswith("#EXTINF:"):
            extinf_line = ln
            continue
        if extinf_line is not None and not ln.startswith("#"):
            res.append((extinf_line, ln))
            extinf_line = None
            continue
        if "," in ln and not ln.startswith("#"):
            sp = ln.split(",", 1)
            name_part = sp[0].strip()
            url_part = sp[1].strip()
            if url_part == "#genre#":
                current_group = name_part
                continue
            if current_group:
                fake_ext = (
                    f'#EXTINF:-1 group-title="{current_group}",{name_part}'
                )
            else:
                fake_ext = f"#EXTINF:-1,{name真不好意思！刚才可能是在生成或传输过程中出现了格式异常，导致内容重复输出了。

你可以告诉我你具体需要的是**哪一段代码或哪种功能**（比如：Python 的某段逻辑、HTML/CSS 布局、前端组件，或是具体的算法实现），我立刻为你重新整理一份干净、完整的源码。
