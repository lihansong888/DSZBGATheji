import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests

# ========== 源地址：(地址, 是否整源都算港台) ==========
# 第二项为 False：按下面的 GROUP_KEYWORDS 从源里的分组名中筛选港台频道
# 第二项为 True：该源本身已按地区拆分（如 iptv-org 的 hk/tw），里面所有频道都保留
SOURCES = [
    ("https://raw.githubusercontent.com/bang359/dsj/refs/heads/main/dsjcs1.txt", False),
    ("https://raw.githubusercontent.com/bj123sd/hycg/refs/heads/main/tv.txt", False),
    ("https://iptv-org.github.io/iptv/countries/hk.m3u", True),
    ("https://iptv-org.github.io/iptv/countries/tw.m3u", True),
]

# ========== 输出分组名 ==========
OUTPUT_GROUP = "HS港澳台直播频道"

# ========== 分组名里包含这些关键词，就认为是港台频道 ==========
GROUP_KEYWORDS = ["香港", "台湾", "台灣", "港澳台", "港台", "🇭🇰", "🇹🇼"]

# ========== 屏蔽频道（频道名包含这些词就丢弃） ==========
BLOCK_CHANNELS = {
    "温馨提醒:设置超时30秒最佳",
}

# ========== 频道别名：把不同叫法统一成一个名字（可自行补充） ==========
ALIASES = {
    "TVB翡翠台": "翡翠台",
    "TVB Jade": "翡翠台",
    "TVB明珠台": "明珠台",
    "TVB Pearl": "明珠台",
    "Viu TV": "ViuTV",
    "VIUTV": "ViuTV",
}

# ========== 检测参数 ==========
MAX_PER_CHANNEL = 3      # 每个频道最多保留几条最快的有效链接
MAX_WORKERS = 32         # 并发检测线程数
CONNECT_TIMEOUT = 4      # 连接超时（秒）
READ_TIMEOUT = 6         # 读取超时（秒）
TOTAL_TIMEOUT = 10       # 单次请求总耗时上限（秒）
# 环境变量 SKIP_CHECK=1 可跳过检测（调试用，或检测环境与观看环境地区不同时）
SKIP_CHECK = os.getenv("SKIP_CHECK") == "1"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; live-checker/1.0)"}


def parse_any(text: str):
    """解析 m3u 或 txt（分组,#genre#）格式，返回 [(频道名, 分组, 链接)]"""
    res = []
    extinf_line = None
    current_group = ""
    for raw_line in text.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        if ln.startswith("#EXTINF:"):
            extinf_line = ln
            continue
        if extinf_line is not None and not ln.startswith("#"):
            name = extinf_line.split(",")[-1].strip() if "," in extinf_line else ""
            m = re.search(r'group-title="([^"]*)"', extinf_line)
            group = m.group(1).strip() if m else ""
            res.append((name, group, ln))
            extinf_line = None
            continue
        if "," in ln and not ln.startswith("#"):
            name_part, url_part = (s.strip() for s in ln.split(",", 1))
            if url_part == "#genre#":
                current_group = name_part
                continue
            res.append((name_part, current_group, url_part))
    return res


def clean_name(name: str) -> str:
    """去掉括号内容和清晰度后缀，并套用别名，让同一频道能合并"""
    name = re.sub(r"[\[\(（【].*?[\]\)）】]", "", name)
    name = re.sub(r"\s*(HD|FHD|UHD|4K|高清|超清|标清)\s*$", "", name, flags=re.I)
    name = name.strip()
    return ALIASES.get(name, name)


def fetch_head(url: str, limit: int = 8192):
    """读取响应开头一小段，返回 (content_type, 数据, 最终地址)；失败返回 None"""
    start = time.time()
    with requests.get(
        url, headers=HEADERS, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True
    ) as r:
        if r.status_code != 200:
            return None
        data = b""
        for chunk in r.iter_content(chunk_size=4096):
            data += chunk
            if len(data) >= limit or time.time() - start > TOTAL_TIMEOUT:
                break
        return r.headers.get("Content-Type", "").lower(), data, r.url


def probe(url: str, depth: int = 0) -> bool:
    """判断链接是否真的能取到内容；m3u8 会继续往下验证到第一个分片"""
    got = fetch_head(url)
    if not got:
        return False
    ctype, data, final_url = got
    if not data or "text/html" in ctype:
        return False

    path = urlparse(url).path.lower()
    if path.endswith(".m3u8") or data.lstrip(b"\xef\xbb\xbf").startswith(b"#EXTM3U"):
        text = data.decode("utf-8", "ignore").lstrip("\ufeff")
        if not text.startswith("#EXTM3U"):
            return False
        if depth >= 2:
            return True
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return False
        # 主播放列表取第一个码率；媒体播放列表取最新的分片
        target = lines[0] if "#EXT-X-STREAM-INF" in text else lines[-1]
        return probe(urljoin(final_url, target), depth + 1)
    return True


def check(url: str):
    """返回响应耗时（秒），不可用返回 None"""
    start = time.time()
    try:
        ok = probe(url)
    except Exception:
        return None
    return time.time() - start if ok else None


def main():
    channels = {}  # 频道名 -> [链接...]，保持首次出现的顺序
    seen = set()

    for src_url, whole_source in SOURCES:
        try:
            resp = requests.get(src_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig", errors="ignore")
        except Exception as e:
            print(f"⚠️ 拉取 {src_url} 失败：{e}")
            continue

        before = sum(len(v) for v in channels.values())
        for raw_name, group, play_url in parse_any(text):
            if not play_url.startswith(("http://", "https://")):
                continue
            name = clean_name(raw_name)
            if not name or any(b in name for b in BLOCK_CHANNELS):
                continue
            if not whole_source and not any(k in group for k in GROUP_KEYWORDS):
                continue
            key = (name, play_url)
            if key in seen:
                continue
            seen.add(key)
            channels.setdefault(name, []).append(play_url)
        after = sum(len(v) for v in channels.values())
        print(f"📥 {src_url} 新增 {after - before} 条候选链接")

    urls = sorted({u for lst in channels.values() for u in lst})
    print(f"🔎 共 {len(channels)} 个频道，{len(urls)} 条链接待检测")

    results = {}
    if SKIP_CHECK:
        results = {u: 0.0 for u in urls}
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(check, u): u for u in urls}
            for f in as_completed(futs):
                results[futs[f]] = f.result()
        alive = sum(1 for v in results.values() if v is not None)
        print(f"✅ 检测完成：{alive}/{len(urls)} 条可用")

    output = ["#EXTM3U"]
    ch_count = 0
    link_count = 0
    for name, lst in channels.items():
        good = sorted((results[u], u) for u in lst if results.get(u) is not None)
        if not good:
            continue
        ch_count += 1
        for _, u in good[:MAX_PER_CHANNEL]:
            output.append(f'#EXTINF:-1 group-title="{OUTPUT_GROUP}",{name}')
            output.append(u)
            link_count += 1

    print(f"📺 输出 {ch_count} 个频道，共 {link_count} 条链接")
    if link_count == 0:
        print("❌ 没有可用链接，不覆盖原文件")
        sys.exit(1)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(out_dir, "live.m3u8")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(output) + "\n")
    print(f"✅ 已输出：{path}")


if __name__ == "__main__":
    main()
