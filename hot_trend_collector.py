"""
热点采集脚本：定时抓取小红书 / 快手 / 抖音热榜，去重排序后生成网页展示。

功能：
  - 定时抓取：小红书热榜、快手热榜、抖音热点榜
  - 字段：标题、热度值、链接、发布时间
  - 去重（按链接或 来源+标题）、按热度排序
  - 生成 hot_trends.html 简单网页展示
  - 每 30 分钟自动执行一次

使用：
  1. 安装依赖：pip install -r requirements-hot-trend.txt
  2. 设置环境变量 ITAPI_KEY（在 https://api.itapi.cn/ 申请）
  3. 运行：python hot_trend_collector.py
  4. 用浏览器打开生成的 hot_trends.html
"""
import os
import time
import json
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime

import requests
import schedule

# ====================== 配置区域 ======================

# 在 https://api.itapi.cn/ 注册账号后，在“密钥管理”里申请 key
# 把下面引号里的内容改成你的密钥（一整段，不要有空格），就不用在终端 export 了
API_KEY = os.getenv("ITAPI_KEY", "VlHQXQA7SveOVijdMBPpsuJ7po")

BASE_URL = "https://api.itapi.cn/api/hotnews"

SOURCES = {
    "douyin": "抖音热榜",
    "kuaishou": "快手热榜",
    "xiaohongshu": "小红书热榜",  # itapi 免费 100 次，用完后需付费或专业会员
}

# 是否只保留关键词相关（留空列表 = 全量采集）
KEYWORD_FILTER: List[str] = []  # 例如 ["摄影", "相机"] 则只保留含这些词的条目

OUTPUT_HTML = "hot_trends.html"
OUTPUT_JSON = "hot_trends.json"
HISTORY_FILE = "hot_trends_history.json"  # 用于生成周榜，仅保留近 7 天
WEEKLY_DAYS = 7
WEEKLY_TOP_N = 50  # 每平台周榜最多显示条数

# ====================== 数据结构定义 ======================

@dataclass
class HotItem:
    source: str          # 平台（抖音 / 快手 / 小红书）
    title: str           # 标题
    heat: int            # 热度值（viewnum 转成 int）
    url: str             # 链接
    publish_time: str    # 发布时间（字符串格式）
    raw: Dict[str, Any]  # 原始数据，方便后续调试/扩展

# ====================== 工具函数 ======================

def safe_int(value: Any, default: int = 0) -> int:
    """解析热度值，支持纯数字或 1100.9w、1.2亿 等格式。"""
    if value is None:
        return default
    try:
        s = str(value).replace(",", "").strip().lower()
        if not s:
            return default
        # 小红书等接口可能返回 "1100.9w"、"1.2亿"
        # 支持 "916.8万"、"1100.9w"、"1.2亿"
        if "万" in s or s.endswith("w"):
            num_s = s.replace("万", "").replace("w", "").strip()
            if num_s:
                return int(float(num_s) * 10000)
        if "亿" in s or s.endswith("y"):
            num_s = s.replace("亿", "").replace("y", "").strip()
            if num_s:
                return int(float(num_s) * 100000000)
        return int(float(s))
    except Exception:
        return default

def match_keywords(text: str) -> bool:
    """无关键词配置时保留全部；有关键词时只保留匹配的。"""
    if not KEYWORD_FILTER:
        return True
    if not text:
        return False
    lower_text = text.lower()
    for kw in KEYWORD_FILTER:
        if kw.lower() in lower_text:
            return True
    return False

# ====================== 抓取函数 ======================

def fetch_from_source(source_key: str, display_name: str, retry_on_502: bool = False) -> List[HotItem]:
    """
    从某个平台抓取热榜，并转为 HotItem 列表。
    兼容形如：
    {
        "code": 200,
        "msg": "success",
        "data": [...]
    }
    """
    url = f"{BASE_URL}/{source_key}"
    params = {"key": API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] 请求 {display_name} 接口失败: {e}")
        return []

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print(f"[ERROR] {display_name} 返回不是合法 JSON，原始内容: {resp.text[:200]}")
        return []

    # 兼容不同结构：顶层 data/result/list，或 data 为 dict 时再取 list/items
    if isinstance(data, dict):
        code = data.get("code")
        if code not in (200, "200", 0, "0", None):
            print(f"[WARN] {display_name} 接口返回错误码: {code}, msg={data.get('msg')}")
            # 502 限流时重试一次（由 collect_by_source 调用时传入 retry_on_502=True）
            if retry_on_502 and code in (502, 429) and "频率" in str(data.get("msg", "")):
                print(f"[INFO] 15 秒后重试 {display_name}...")
                time.sleep(15)
                return fetch_from_source(source_key, display_name, retry_on_502=False)
        items = data.get("data") or data.get("result") or data.get("list") or data.get("info") or []
        if isinstance(items, dict):
            items = items.get("list") or items.get("items") or items.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        print(f"[WARN] {display_name} 返回未知结构: {type(data)}")
        return []

    # 小红书等接口无数据时：打印响应结构便于排查
    if isinstance(data, dict) and not items:
        keys = list(data.keys())
        print(f"[DEBUG] {display_name} 返回列表为空，响应键: {keys}")
        # 若有 data 且是 dict，打印其键（可能是 data.list 等嵌套）
        inner = data.get("data")
        if isinstance(inner, dict):
            print(f"[DEBUG] {display_name} data 内部键: {list(inner.keys())}")

    result: List[HotItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        title = item.get("name") or item.get("title") or ""
        heat = safe_int(item.get("viewnum") or item.get("hot") or item.get("heat") or 0)
        link = item.get("url") or item.get("link") or ""
        publish_time = item.get("date") or item.get("time") or item.get("publish_time") or ""

        if not match_keywords(title):
            continue

        result.append(
            HotItem(
                source=display_name,
                title=title,
                heat=heat,
                url=link,
                publish_time=publish_time,
                raw=item,
            )
        )

    print(f"[INFO] {display_name} 抓取到 {len(result)} 条数据。")
    return result

# ====================== 聚合、去重、排序 ======================

def collect_all() -> List[HotItem]:
    """合并所有平台，去重后按热度排序（用于 JSON 等）。"""
    all_items: List[HotItem] = []

    for key, name in SOURCES.items():
        items = fetch_from_source(key, name)
        all_items.extend(items)

    dedup_map: Dict[str, HotItem] = {}
    for item in all_items:
        if item.url:
            k = item.url
        else:
            k = f"{item.source}::{item.title}"

        if k in dedup_map:
            if item.heat > dedup_map[k].heat:
                dedup_map[k] = item
        else:
            dedup_map[k] = item

    dedup_list = list(dedup_map.values())
    dedup_list.sort(key=lambda x: x.heat, reverse=True)
    print(f"[INFO] 总共保留 {len(dedup_list)} 条（已去重并按热度排序）。")
    return dedup_list


def collect_by_source() -> Dict[str, List[HotItem]]:
    """按平台分别抓取，返回 平台名 -> 该平台列表（按热度排序）。"""
    result: Dict[str, List[HotItem]] = {}
    for key, name in SOURCES.items():
        items = fetch_from_source(key, name, retry_on_502=True)
        items.sort(key=lambda x: x.heat, reverse=True)
        result[name] = items
        # 间隔 2 秒再请求下一平台，降低触发「请求频率超过限制」502 的概率
        if key != list(SOURCES.keys())[-1]:
            time.sleep(2)
    return result


# ====================== 周榜：历史积累 + 聚合 ======================

def _history_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), HISTORY_FILE)


def save_history(by_source: Dict[str, List[HotItem]]) -> None:
    """把本轮抓取结果追加到历史文件，只保留近 WEEKLY_DAYS 天。"""
    from datetime import timedelta
    now = datetime.now()
    record = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "items": [asdict(item) for items in by_source.values() for item in items],
    }
    cutoff = (now - timedelta(days=WEEKLY_DAYS)).strftime("%Y-%m-%d")
    history: List[Dict[str, Any]] = []
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(record)
    history = [r for r in history if (r.get("ts") or "")[:10] >= cutoff]
    with open(_history_path(), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=0)
    print(f"[INFO] 历史已写入（共 {len(history)} 次抓取，用于周榜）。")


def load_weekly_aggregate() -> Dict[str, List[HotItem]]:
    """从历史中取近 7 天数据，按 (平台, 链接/标题) 聚合热度求和，按平台排序取前 WEEKLY_TOP_N。"""
    from datetime import timedelta
    now = datetime.now()
    cutoff = (now - timedelta(days=WEEKLY_DAYS)).strftime("%Y-%m-%d")
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {name: [] for name in SOURCES.values()}
    history = [r for r in history if isinstance(r, dict) and (r.get("ts") or "")[:10] >= cutoff]
    if not history:
        return {name: [] for name in SOURCES.values()}
    # 聚合：(source, key) -> { total_heat, title, url, publish_time }
    agg: Dict[str, Dict[str, Any]] = {}
    for record in history:
        for raw in record.get("items") or []:
            if not isinstance(raw, dict):
                continue
            source = raw.get("source") or ""
            title = (raw.get("title") or "").strip()
            url = (raw.get("url") or "").strip()
            heat = safe_int(raw.get("heat"), 0)
            publish_time = raw.get("publish_time") or ""
            key = url if url else f"{source}::{title}"
            if key not in agg:
                agg[key] = {"source": source, "title": title, "url": url, "publish_time": publish_time, "heat": 0}
            agg[key]["heat"] += heat
    # 按平台分组，按热度排序，取前 N
    result: Dict[str, List[HotItem]] = {name: [] for name in SOURCES.values()}
    by_source: Dict[str, List[Dict]] = {name: [] for name in SOURCES.values()}
    for v in agg.values():
        src = v.get("source") or ""
        if src in by_source:
            by_source[src].append(v)
    for name in SOURCES.values():
        items = by_source.get(name, [])
        items.sort(key=lambda x: x.get("heat") or 0, reverse=True)
        for item in items[:WEEKLY_TOP_N]:
            result[name].append(HotItem(
                source=name,
                title=item.get("title") or "",
                heat=item.get("heat") or 0,
                url=item.get("url") or "",
                publish_time=item.get("publish_time") or "",
                raw=item,
            ))
    return result


# ====================== 生成 HTML 页面 ======================

def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_heat(heat: int) -> str:
    """热度值展示：大数用 万/亿，避免一长串 0。"""
    if heat >= 100000000:
        return f"{heat / 100000000:.1f}亿"
    if heat >= 10000:
        return f"{heat / 10000:.1f}万"
    return str(heat)


def _render_table_rows(items: List[HotItem]) -> str:
    """把 HotItem 列表渲染成表格 tbody 行。"""
    rows = []
    for idx, item in enumerate(items, start=1):
        safe_url = item.url or "#"
        safe_title = _escape(item.title)
        safe_time = item.publish_time or "-"
        heat_display = _format_heat(item.heat)
        rows.append(
            f"<tr>"
            f"<td>{idx}</td>"
            f"<td><a href=\"{safe_url}\" target=\"_blank\">{safe_title}</a></td>"
            f"<td class=\"heat-cell\">{heat_display}</td>"
            f"<td>{safe_time}</td>"
            f"</tr>"
        )
    return "".join(rows)


def generate_html(
    items_by_source: Dict[str, List[HotItem]],
    weekly_by_source: Optional[Dict[str, List[HotItem]]] = None,
    output_path: str = OUTPUT_HTML,
) -> None:
    """按平台分块生成 HTML：各平台含热榜 + 周榜（若有历史数据）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = sum(len(items) for items in items_by_source.values())
    if weekly_by_source is None:
        weekly_by_source = {}

    # 固定顺序：抖音、快手、小红书
    order = list(SOURCES.values())
    tab_buttons = []
    sections_html = []

    for i, source_name in enumerate(order):
        items = items_by_source.get(source_name, [])
        weekly_items = weekly_by_source.get(source_name, [])
        active = " active" if i == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn{active}" data-tab="{i}" type="button">{source_name}</button>'
        )
        hot_table_body = _render_table_rows(items)
        hot_block = f"""
            <h3 class="block-title">热榜</h3>
            <p class="platform-meta">共 {len(items)} 条 · 按热度排序</p>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>标题</th>
                            <th>热度值</th>
                            <th>发布时间</th>
                        </tr>
                    </thead>
                    <tbody>
                        {hot_table_body}
                    </tbody>
                </table>
            </div>"""
        if weekly_items:
            weekly_table_body = _render_table_rows(weekly_items)
            weekly_block = f"""
            <h3 class="block-title">周榜</h3>
            <p class="platform-meta">近 {WEEKLY_DAYS} 天热度汇总 · 共 {len(weekly_items)} 条</p>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>标题</th>
                            <th>热度值</th>
                            <th>发布时间</th>
                        </tr>
                    </thead>
                    <tbody>
                        {weekly_table_body}
                    </tbody>
                </table>
            </div>"""
        else:
            weekly_block = """
            <h3 class="block-title">周榜</h3>
            <p class="platform-meta">暂无数据，需持续运行脚本积累近 7 天抓取后生成。</p>"""
        panel_active = " tab-panel-active" if i == 0 else ""
        sections_html.append(
            f"""
        <section class="tab-panel{panel_active}" id="panel-{i}" data-tab="{i}">
            {hot_block}
            <div class="weekly-section">
                {weekly_block}
            </div>
        </section>"""
        )

    tab_bar_html = '<div class="tab-bar">' + "".join(tab_buttons) + "</div>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>热点聚合榜 - 抖音 / 快手 / 小红书</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #f2f0eb;
            --card: #ffffff;
            --text: #1c1917;
            --text-muted: #78716c;
            --accent: #c2410c;
            --accent-soft: #fff7ed;
            --border: #e7e5e4;
            --hover: #fafaf9;
        }}
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: "Noto Sans SC", -apple-system, sans-serif;
            background: var(--bg);
            margin: 0;
            padding: 24px 16px;
            min-height: 100vh;
            color: var(--text);
        }}
        .container {{
            max-width: 960px;
            margin: 0 auto;
            background: var(--card);
            padding: 32px 36px;
            border-radius: 24px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.04);
        }}
        h1 {{
            margin: 0 0 6px 0;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: var(--text);
        }}
        .meta {{
            color: var(--text-muted);
            font-size: 13px;
            margin-bottom: 24px;
            font-weight: 500;
        }}
        .tab-bar {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 28px;
        }}
        .tab-btn {{
            padding: 12px 22px;
            font-size: 15px;
            font-weight: 600;
            font-family: inherit;
            color: var(--text-muted);
            background: var(--hover);
            border: 1px solid var(--border);
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        .tab-btn:hover {{
            color: var(--text);
            background: #f5f5f4;
            border-color: #d6d3d1;
        }}
        .tab-btn.active {{
            color: #fff;
            background: var(--accent);
            border-color: var(--accent);
        }}
        .tab-panel {{
            display: none;
            animation: fadeIn 0.25s ease;
        }}
        .tab-panel.tab-panel-active {{
            display: block;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; }}
            to {{ opacity: 1; }}
        }}
        .block-title {{
            font-size: 16px;
            font-weight: 600;
            color: var(--text);
            margin: 0 0 8px 0;
        }}
        .weekly-section {{
            margin-top: 28px;
            padding-top: 20px;
            border-top: 1px dashed var(--border);
        }}
        .platform-meta {{
            color: var(--text-muted);
            font-size: 13px;
            margin: 0 0 16px 0;
            font-weight: 500;
        }}
        .table-wrap {{
            overflow-x: auto;
            border-radius: 14px;
            border: 1px solid var(--border);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        thead {{
            background: linear-gradient(180deg, #fafaf9 0%, #f5f5f4 100%);
        }}
        th {{
            padding: 14px 16px;
            text-align: left;
            font-weight: 600;
            color: var(--text);
            white-space: nowrap;
            font-size: 13px;
            letter-spacing: 0.02em;
        }}
        th:first-child {{
            border-radius: 14px 0 0 0;
        }}
        th:last-child {{
            border-radius: 0 14px 0 0;
        }}
        td {{
            padding: 14px 16px;
            border-top: 1px solid var(--border);
            transition: background 0.15s ease;
        }}
        tbody tr:hover {{
            background: var(--accent-soft);
        }}
        tbody tr:last-child td:first-child {{
            border-radius: 0 0 0 14px;
        }}
        tbody tr:last-child td:last-child {{
            border-radius: 0 0 14px 0;
        }}
        td:first-child {{
            color: var(--text-muted);
            font-weight: 600;
            width: 48px;
        }}
        a {{
            color: var(--accent);
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            text-decoration: underline;
        }}
        .heat-cell {{
            font-weight: 600;
            color: var(--text);
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>热点聚合榜</h1>
        <div class="meta">
            最近更新：{now} · 共 {total} 条
        </div>
        {tab_bar_html}
        {''.join(sections_html)}
    </div>
    <script>
        document.querySelectorAll(".tab-btn").forEach(function(btn) {{
            btn.addEventListener("click", function() {{
                var t = this.getAttribute("data-tab");
                document.querySelectorAll(".tab-btn").forEach(function(b) {{ b.classList.remove("active"); }});
                document.querySelectorAll(".tab-panel").forEach(function(p) {{ p.classList.remove("tab-panel-active"); }});
                this.classList.add("active");
                var panel = document.getElementById("panel-" + t);
                if (panel) panel.classList.add("tab-panel-active");
            }});
        }});
    </script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[INFO] HTML 已生成：{output_path}")

# ====================== 主任务函数 ======================

def job():
    print("=" * 60)
    print(f"[INFO] 开始抓取热点：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    # 按平台分开抓取，用于分块展示
    by_source = collect_by_source()
    total = sum(len(items) for items in by_source.values())

    # 合并列表写入 JSON（兼容旧用法）
    all_items = []
    for name in SOURCES.values():
        all_items.extend(by_source.get(name, []))
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump([asdict(i) for i in all_items], f, ensure_ascii=False, indent=2)
    print(f"[INFO] JSON 数据已写入：{OUTPUT_JSON}")

    save_history(by_source)
    weekly_by_source = load_weekly_aggregate()
    generate_html(by_source, weekly_by_source, OUTPUT_HTML)
    print(f"[INFO] 本轮任务完成，共 {total} 条。")

# ====================== 调度：每 30 分钟一次 ======================

def main():
    # 先执行一次
    job()

    # 使用 schedule 每 30 分钟执行一次
    schedule.every(30).minutes.do(job)

    print("[INFO] 已启动定时任务（每 30 分钟一次），按 Ctrl+C 退出。")
    while True:
        schedule.run_pending()
        time.sleep(5)

if __name__ == "__main__":
    main()