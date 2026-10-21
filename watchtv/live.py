"""港澳台直播源聚合 + 真实可用性检测

流程：拉取多个源 -> 筛出港澳台频道 -> 并发检测每条链接 -> 每个频道只留最快的几条 -> 输出 m3u8 / txt

检测的不只是"能不能连上"，还包括：
  * 下载速度：HLS 取最新一个分片，要求下载速度达到播放速度的 MIN_RATIO 倍（解决"转圈加载"）
  * 画面：用 ffprobe 检查确有视频流、分辨率不低于 MIN_HEIGHT（解决"黑屏/只有声音"）
  * 直播：丢弃已结束的点播列表；丢弃需要特殊请求头（Referer/UA）才能播的条目
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutTimeout
from urllib.parse import urljoin

import requests

# ========== 源地址：(地址, 整个源是否都算港澳台) ==========
# True ：该源本身按地区拆分（如 iptv-org 的 hk/mo/tw），里面所有频道都保留
# False：综合源，按下面的 GROUP_KEYWORDS 从分组名里筛选港澳台频道
SOURCES = [
    ("https://iptv-org.github.io/iptv/countries/hk.m3u", True),
    ("https://iptv-org.github.io/iptv/countries/mo.m3u", True),
    ("https://iptv-org.github.io/iptv/countries/tw.m3u", True),
    ("https://guovin.github.io/iptv-api/result.m3u", False),
    ("https://raw.githubusercontent.com/bang359/dsj/refs/heads/main/dsjcs1.txt", False),
    ("https://raw.githubusercontent.com/bj123sd/hycg/refs/heads/main/tv.txt", False),
]

# ========== 输出分组名 ==========
OUTPUT_GROUP = "HS港澳台直播频道"

# ========== 分组名里包含这些关键词，就认为是港澳台频道 ==========
GROUP_KEYWORDS = ["香港", "台湾", "台灣", "澳门", "澳門", "港澳台", "港台", "🇭🇰", "🇹🇼", "🇲🇴"]

# ========== 屏蔽 ==========
BLOCK_CHANNELS = {"温馨提醒:设置超时30秒最佳"}   # 频道名包含这些词就丢弃
BLOCK_URL_KEYWORDS = []                          # 链接里包含这些词就丢弃（可填确认有问题的域名）
SKIP_TAGS = ("Geo-blocked", "Not 24/7")          # 源里标注了地区限制/非全天的条目直接跳过
SKIP_NEEDS_HEADERS = True                        # 需要 Referer/UA 才能播放的条目直接跳过（多数播放器不支持）

# ========== 频道别名：把不同叫法统一成一个名字（可自行补充） ==========
ALIASES = {
    "TVB翡翠台": "翡翠台",
    "TVB Jade": "翡翠台",
    "TVB明珠台": "明珠台",
    "TVB Pearl": "明珠台",
    "Viu TV": "ViuTV",
    "VIUTV": "ViuTV",
}

# ========== 检测与筛选参数 ==========
MAX_PER_CHANNEL = 3        # 每个频道最多保留几条（按速度从快到慢）
MIN_RATIO = 1.5            # HLS：分片下载速度至少是播放速度的几倍，越大越严格
MIN_HEIGHT = 480           # 视频高度下限（像素），0 表示不限制
REJECT_VOD = True          # 丢弃已结束的点播列表（带 #EXT-X-ENDLIST）
MAX_WORKERS = 16           # 并发检测线程数
CONNECT_TIMEOUT = 4        # 连接超时（秒）
READ_TIMEOUT = 6           # 单次读取超时（秒）
SEG_TIMEOUT = 12           # 下载一个分片的总时限（秒）
SAMPLE_SECONDS = 5         # 非 HLS 直链的采样时长（秒）
MIN_DIRECT_BYTES = 96 * 1024       # 直链采样期内至少要收到多少字节
MAX_SEG_BYTES = 12 * 1024 * 1024   # 分片最大读取量
MAX_DIRECT_BYTES = 1500 * 1024     # 直链最大采样量
GLOBAL_DEADLINE = 15 * 60  # 检测总时限（秒），超时的链接按失败处理

# 环境变量：SKIP_CHECK=1 跳过检测（调试用）；FORCE=1 允许结果比上次少很多时仍然覆盖
SKIP_CHECK = os.getenv("SKIP_CHECK") == "1"
FORCE = os.getenv("FORCE") == "1"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; live-checker/2.0)"}
FFPROBE = shutil.which("ffprobe")


class Fail(Exception):
    """检测失败，message 即失败原因"""


Resp = namedtuple("Resp", "data ctype url ttfb elapsed complete timed_out")


# ---------------------------------------------------------------- 解析 ----
def parse_any(text: str):
    """解析 m3u 或 txt（分组,#genre#）格式，返回 [(频道名, 分组, 链接, 是否需要请求头)]"""
    res = []
    extinf = None
    needs_headers = False
    txt_group = ""
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#EXTINF:"):
            extinf, needs_headers = ln, False
            continue
        if ln.startswith("#EXTVLCOPT:"):
            low = ln.lower()
            if any(k in low for k in ("http-referrer", "http-referer", "http-user-agent", "http-origin")):
                needs_headers = True
            continue
        if extinf is not None and not ln.startswith("#"):
            name = extinf.split(",")[-1].strip() if "," in extinf else ""
            m = re.search(r'group-title="([^"]*)"', extinf)
            res.append((name, m.group(1).strip() if m else "", ln, needs_headers))
            extinf, needs_headers = None, False
            continue
        if "," in ln and not ln.startswith("#"):
            name_part, url_part = (s.strip() for s in ln.split(",", 1))
            if url_part == "#genre#":
                txt_group = name_part
                continue
            res.append((name_part, txt_group, url_part, False))
    return res


def clean_name(name: str) -> str:
    """去掉括号内容和清晰度后缀，并套用别名，让同一频道能合并"""
    name = re.sub(r"[\[\(（【].*?[\]\)）】]", "", name)
    name = re.sub(r"\s*(HD|FHD|UHD|4K|高清|超清|标清)\s*$", "", name, flags=re.I)
    name = name.strip()
    return ALIASES.get(name, name)


# ---------------------------------------------------------------- 检测 ----
def http_get(url: str, max_bytes: int, total_timeout: float) -> Resp:
    """流式读取，最多 max_bytes 字节或 total_timeout 秒"""
    start = time.time()
    with requests.get(
        url, headers=HEADERS, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True
    ) as r:
        if r.status_code != 200:
            raise Fail(f"http-{r.status_code}")
        ctype = r.headers.get("Content-Type", "").lower()
        data = bytearray()
        ttfb = None
        complete, timed_out = True, False
        for chunk in r.iter_content(chunk_size=16384):
            if not chunk:
                continue
            if ttfb is None:
                ttfb = time.time() - start
            data += chunk
            if len(data) >= max_bytes:
                complete = False
                break
            if time.time() - start > total_timeout:
                complete, timed_out = False, True
                break
        if not data:
            raise Fail("empty")
        return Resp(bytes(data), ctype, r.url, ttfb, time.time() - start, complete, timed_out)


def check_media(data: bytes) -> int:
    """用 ffprobe 检查画面。返回视频高度（未知为 0）；无画面、分辨率过低、明显损坏时抛 Fail"""
    if not FFPROBE:
        return 0
    path, streams = None, None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            path = f.name
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height", "-of", "json", path],
            capture_output=True, timeout=15,
        )
        if p.returncode == 0:
            streams = json.loads(p.stdout or b"{}").get("streams", [])
    except Exception:
        streams = None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    if streams is None:  # ffprobe 无法解析
        if data[:1] == b"\x47" or data[:3] == b"FLV":  # 明明是 TS/FLV 却解析失败 -> 数据有问题
            raise Fail("bad-media")
        return 0  # 其它封装（如 fMP4 缺少初始化段）无法判断，放行
    if not streams:
        raise Fail("no-video")
    height = int(streams[0].get("height") or 0)
    if MIN_HEIGHT and height and height < MIN_HEIGHT:
        raise Fail("low-res")
    return height


def probe_hls(url: str, text: str, depth: int = 0):
    """验证 HLS：下载最新分片，计算下载速度/播放速度，并检查画面。返回 (倍率, 高度)"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 主播放列表：取码率最高的一路（最严格）继续往下验证
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        if depth >= 2:
            raise Fail("nested")
        best, best_bw = None, -1
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", l)
                bw = int(m.group(1)) if m else 0
                nxt = next((x for x in lines[i + 1:] if not x.startswith("#")), None)
                if nxt and bw > best_bw:
                    best, best_bw = nxt, bw
        if best is None:
            raise Fail("no-variant")
        r = http_get(urljoin(url, best), 262144, 8)
        t = r.data.lstrip(b"\xef\xbb\xbf").decode("utf-8", "ignore")
        if not t.startswith("#EXTM3U"):
            raise Fail("not-playlist")
        return probe_hls(r.url, t, depth + 1)

    # 媒体播放列表
    segs, dur, encrypted = [], 0.0, False
    for l in lines:
        if l.startswith("#EXT-X-KEY"):
            if "METHOD=NONE" not in l:
                encrypted = True
        elif l.startswith("#EXTINF:"):
            try:
                dur = float(l[8:].split(",")[0])
            except ValueError:
                dur = 0.0
        elif not l.startswith("#"):
            segs.append((dur, urljoin(url, l)))
            dur = 0.0
    if not segs:
        raise Fail("no-segments")
    if REJECT_VOD and any(l.startswith("#EXT-X-ENDLIST") for l in lines):
        raise Fail("vod")

    seg_dur, seg_url = segs[-1]  # 最新的分片
    if seg_dur <= 0:
        seg_dur = 6.0
    r = http_get(seg_url, MAX_SEG_BYTES, SEG_TIMEOUT)
    if r.timed_out:
        raise Fail("slow")
    if "text/html" in r.ctype:
        raise Fail("html")
    ratio = seg_dur / max(r.elapsed, 0.05)
    if ratio < MIN_RATIO:
        raise Fail("slow")
    height = 0 if encrypted else check_media(r.data)
    return ratio, height


def probe_direct(url: str):
    """验证非 HLS 直链：采样几秒，要求有足够数据且画面正常。返回 (1.0, 高度)"""
    r = http_get(url, MAX_DIRECT_BYTES, SAMPLE_SECONDS)
    if "text/html" in r.ctype:
        raise Fail("html")
    if len(r.data) < MIN_DIRECT_BYTES:
        raise Fail("slow")
    return 1.0, check_media(r.data)


def check(url: str) -> dict:
    t0 = time.time()
    try:
        r = http_get(url, 65536, 8)
        if "text/html" in r.ctype:
            raise Fail("html")
        head = r.data.lstrip(b"\xef\xbb\xbf")
        if head.startswith(b"#EXTM3U"):
            score, height = probe_hls(r.url, head.decode("utf-8", "ignore"))
        else:
            score, height = probe_direct(url)
        return {"ok": True, "score": score, "latency": time.time() - t0, "h": height}
    except Fail as e:
        return {"ok": False, "reason": str(e)}
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "timeout"}
    except requests.exceptions.RequestException:
        return {"ok": False, "reason": "unreachable"}
    except Exception:
        return {"ok": False, "reason": "error"}


# ---------------------------------------------------------------- 主流程 ----
def count_extinf(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for l in f if l.startswith("#EXTINF"))
    except OSError:
        return 0


def main():
    channels = {}  # 频道名 -> [链接...]，保持首次出现的顺序
    seen = set()
    skipped = Counter()

    for src_url, whole_source in SOURCES:
        try:
            resp = requests.get(src_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig", errors="ignore")
        except Exception as e:
            print(f"⚠️ 拉取 {src_url} 失败：{e}")
            continue

        before = sum(len(v) for v in channels.values())
        for raw_name, group, play_url, needs_headers in parse_any(text):
            if not play_url.startswith(("http://", "https://")):
                continue
            if any(t.lower() in raw_name.lower() for t in SKIP_TAGS):
                skipped["标注地区限制/非全天"] += 1
                continue
            if SKIP_NEEDS_HEADERS and needs_headers:
                skipped["需要特殊请求头"] += 1
                continue
            if any(k in play_url for k in BLOCK_URL_KEYWORDS):
                skipped["命中链接黑名单"] += 1
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
        print(f"📥 {src_url} 新增 {after - before} 条候选")

    if skipped:
        print("🚫 预先跳过：" + "，".join(f"{k} {v} 条" for k, v in skipped.items()))

    urls = sorted({u for lst in channels.values() for u in lst})
    print(f"🔎 共 {len(channels)} 个频道，{len(urls)} 条链接待检测")

    results = {}
    if SKIP_CHECK:
        print("⏭️ SKIP_CHECK=1，跳过检测")
        results = {u: {"ok": True, "score": 0.0, "latency": 0.0, "h": 0} for u in urls}
    else:
        if not FFPROBE:
            print("⚠️ 未找到 ffprobe，将不检查画面（黑屏链接可能漏网）")
        ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        futs = {ex.submit(check, u): u for u in urls}
        try:
            for f in as_completed(futs, timeout=GLOBAL_DEADLINE):
                results[futs[f]] = f.result()
        except FutTimeout:
            print("⚠️ 达到总时限，剩余链接按失败处理")
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        alive = sum(1 for v in results.values() if v["ok"])
        reasons = Counter(v["reason"] for v in results.values() if not v["ok"])
        if len(urls) > len(results):
            reasons["deadline"] += len(urls) - len(results)
        print(f"✅ 检测完成：{alive}/{len(urls)} 条可用")
        if reasons:
            print("❌ 失败原因：" + "，".join(f"{k} {v}" for k, v in reasons.most_common()))

    m3u = ["#EXTM3U"]
    txt = [f"{OUTPUT_GROUP},#genre#"]
    ch_count = link_count = 0
    for name, lst in channels.items():
        good = [(results[u], u) for u in lst if results.get(u, {}).get("ok")]
        if not good:
            continue
        good.sort(key=lambda x: (-x[0]["score"], x[0]["latency"]))
        ch_count += 1
        for res, u in good[:MAX_PER_CHANNEL]:
            m3u.append(f'#EXTINF:-1 group-title="{OUTPUT_GROUP}",{name}')
            m3u.append(u)
            txt.append(f"{name},{u}")
            link_count += 1
        best = good[0][0]
        detail = f"{best['score']:.1f}x 实时速度" if best["score"] else "未检测"
        if best["h"]:
            detail += f"，{best['h']}p"
        print(f"  - {name}: 保留 {min(len(good), MAX_PER_CHANNEL)}/{len(lst)} 条（最佳 {detail}）")

    print(f"📺 输出 {ch_count} 个频道，共 {link_count} 条链接")
    if link_count == 0:
        print("❌ 没有可用链接，不覆盖原文件")
        sys.exit(1)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    m3u_path = os.path.join(out_dir, "live.m3u8")
    txt_path = os.path.join(out_dir, "live.txt")

    old = count_extinf(m3u_path)
    if not FORCE and old >= 10 and link_count < old * 0.3:
        print(f"❌ 本次只有 {link_count} 条，而上次有 {old} 条，疑似网络异常，不覆盖。确认无误可设置 FORCE=1")
        sys.exit(1)

    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt) + "\n")
    print(f"✅ 已输出：{m3u_path} 和 {txt_path}")


if __name__ == "__main__":
    main()
