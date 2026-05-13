#!/usr/bin/env python3
"""
招聘岗位信息收集器
每天自动从多个来源爬取岗位信息，汇总为 Markdown 表格。
"""

import os
import re
import sys
import json
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

import yaml
import requests

# ════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    date: str = ""        # 发布日期（原始字符串）
    summary: str = ""      # 简短描述

    @property
    def fingerprint(self) -> str:
        """用于去重"""
        raw = f"{self.title}|{self.company}|{self.url}"
        return hashlib.md5(raw.encode()).hexdigest()


# ════════════════════════════════════════════════════════════════
# RSS 解析器
# ════════════════════════════════════════════════════════════════

def parse_rss(xml_text: str, source_name: str) -> list[Job]:
    """解析标准 RSS 2.0 XML，返回 Job 列表"""
    jobs = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  [warn] RSS 解析失败: {e}")
        return jobs

    # RSS 的 namespace 处理
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    channel = root.find("channel")
    if channel is None:
        return jobs

    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        date_el = item.find("pubDate")
        company = _extract_company(title_el.text if title_el is not None else "",
                                   desc_el.text if desc_el is not None else "")

        job = Job(
            title=_clean_html(title_el.text) if title_el is not None else "",
            company=company,
            location="",
            url=link_el.text if link_el is not None else "",
            source=source_name,
            date=date_el.text.strip() if date_el is not None and date_el.text else "",
            summary=(_clean_html(desc_el.text[:300])
                     if desc_el is not None and desc_el.text else ""),
        )
        if job.title and job.url:
            jobs.append(job)

    return jobs


def _extract_company(title: str, description: str) -> str:
    """从 RSS Item 中尽量提取公司名"""
    # Indeed RSS 格式: description 里通常包含公司名
    patterns = [
        r"Company:\s*([^<]+)",
        r"\- ([^<]+?)\s*-",
    ]
    for pat in patterns:
        m = re.search(pat, description)
        if m:
            return m.group(1).strip()
    return ""


def _clean_html(text: str) -> str:
    """移除 HTML 标签和多余空白"""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ════════════════════════════════════════════════════════════════
# 数据源
# ════════════════════════════════════════════════════════════════

def _matches_keywords(text: str, keywords: list[str]) -> bool:
    """检查文本是否匹配任一关键词（支持短语匹配和单词匹配）"""
    text_lower = text.lower()
    for kw in keywords:
        kw = kw.lower().strip()
        if kw in text_lower:
            return True
        # 如果关键词是多个单词，也尝试分别匹配每个单词
        words = kw.split()
        if len(words) > 1 and all(w in text_lower for w in words):
            return True
    return False


def fetch_indeed(keywords: list[str], location: str, limit: int = 25) -> list[Job]:
    """从 Indeed RSS 拉取岗位（可能被 Cloudflare 拦截）"""
    query = "+".join(keywords)
    urls = [
        f"https://rss.indeed.com/rss?q={query}&limit={limit}",
        f"https://www.indeed.com/rss?q={query}&limit={limit}",
        f"https://www.indeed.co.uk/rss?q={query}&limit={limit}",
    ]
    if location:
        urls = [f"{u}&l={location}" for u in urls]

    for url in urls:
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            jobs = parse_rss(resp.text, "Indeed")
            if jobs:
                print(f"  Indeed: ✓ 获取 {len(jobs)} 个岗位")
                return jobs
        except requests.RequestException:
            continue
    print(f"  Indeed: ✗ 不可用（Cloudflare 防护）")
    return []


def fetch_remoteok(keywords: list[str], limit: int = 25) -> list[Job]:
    """从 RemoteOK API 拉取远程技术岗位"""
    url = "https://remoteok.com/api"
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data[1:] if isinstance(data, list) and len(data) > 0 else []

        jobs = []
        for item in raw_jobs[:limit]:
            title = item.get("position", "") or ""
            desc = item.get("description", "") or ""
            raw_tags = item.get("tags", [])
            if raw_tags and isinstance(raw_tags[0], str):
                tags = raw_tags
            else:
                tags = [t.get("text", "") for t in raw_tags if isinstance(t, dict)]
            combined = f"{title} {desc} {' '.join(tags)}"

            if not _matches_keywords(combined, keywords):
                continue

            job = Job(
                title=title,
                company=item.get("company", "") or "",
                location=item.get("location", "Remote") or "Remote",
                url=item.get("url", "") or "",
                source="RemoteOK",
                date=item.get("date", "") or "",
                summary=_clean_html(desc[:150]),
            )
            if job.title and job.url:
                jobs.append(job)

        print(f"  RemoteOK: ✓ 获取 {len(jobs)} 个岗位")
        return jobs
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  RemoteOK: ✗ 请求失败: {e}")
        return []


def fetch_jobicy(keywords: list[str], limit: int = 25) -> list[Job]:
    """从 Jobicy API 拉取远程岗位（免费、无需 Key）"""
    tags = ["backend", "frontend", "fullstack", "devops", "engineering"]
    jobs = []
    seen_ids = set()

    for tag in tags:
        url = f"https://jobicy.com/api/v2/remote-jobs?count={limit}&tag={tag}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("jobs", []):
                jid = item.get("id")
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                title = item.get("jobTitle", "") or ""
                desc = item.get("jobDescription", "") or ""
                excerpt = item.get("jobExcerpt", "") or ""
                combined = f"{title} {excerpt} {desc[:500]}"

                if not _matches_keywords(combined, keywords):
                    continue

                job = Job(
                    title=title,
                    company=item.get("companyName", "") or "",
                    location=item.get("jobGeo", "Remote") or "Remote",
                    url=item.get("url", "") or "",
                    source="Jobicy",
                    date=item.get("pubDate", "") or "",
                    summary=_clean_html(excerpt[:200]),
                )
                if job.title and job.url:
                    jobs.append(job)
        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            print(f"  Jobicy({tag}): ✗ {e}")
            continue

    # 去重
    seen = set()
    unique = []
    for j in jobs:
        fp = j.fingerprint
        if fp not in seen:
            seen.add(fp)
            unique.append(j)

    print(f"  Jobicy: ✓ 获取 {len(unique)} 个岗位")
    return unique


# ════════════════════════════════════════════════════════════════
# 格式化输出
# ════════════════════════════════════════════════════════════════

def render_markdown(jobs: list[Job], date_str: str) -> str:
    """将岗位列表渲染为 Markdown 表格"""
    lines = []
    lines.append(f"# 招聘岗位汇总 - {date_str}")
    lines.append("")
    lines.append(f"> 共找到 **{len(jobs)}** 个匹配岗位")
    lines.append("")

    if not jobs:
        lines.append("_暂无匹配岗位，请检查关键词配置或稍后重试。_")
        lines.append("")
        return "\n".join(lines)

    # 按来源分组
    from itertools import groupby
    jobs_sorted = sorted(jobs, key=lambda j: j.source)
    groups = {k: list(v) for k, v in groupby(jobs_sorted, key=lambda j: j.source)}

    for source_name, group in groups.items():
        lines.append(f"## 📍 {source_name}")
        lines.append("")
        lines.append("| # | 公司 | 岗位 | 地点 | 详情 |")
        lines.append("|---|---|---|---|---|")
        for idx, job in enumerate(group, 1):
            company = job.company or "-"
            loc = job.location or "-"
            lines.append(
                f"| {idx} | {company} | {job.title} | {loc} | [查看]({job.url}) |"
            )
        lines.append("")

    lines.append("---")
    lines.append(f"_自动生成于 {date_str} · 数据来源: {', '.join(groups.keys())}_")
    lines.append("")

    return "\n".join(lines)


def save_report(markdown: str, output_dir: str, date_str: str):
    """保存日报到文件，同时更新 README.md 索引"""
    os.makedirs(output_dir, exist_ok=True)

    # 保存日报
    filepath = os.path.join(output_dir, f"{date_str}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"\n✅ 日报已保存: {filepath}")
    return filepath


# ════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_date_str(tz_name: str = "Asia/Shanghai") -> str:
    """获取当前日期字符串（上海时区）"""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(tz_name)
        now = datetime.now(tz)
    except Exception:
        # fallback: UTC+8
        now = datetime.now(timezone(timedelta(hours=8)))
    return now.strftime("%Y-%m-%d")


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "config.yaml")

    if not os.path.exists(config_path):
        print(f"[错误] 配置文件不存在: {config_path}")
        sys.exit(1)

    config = load_config(config_path)
    keywords = config.get("keywords", [])
    location = config.get("location", "")
    max_results = config.get("max_results_per_source", 25)
    date_str = get_date_str(config.get("date_format", "Asia/Shanghai"))
    output_dir = os.path.join(base_dir, "output")

    print(f"🔍 招聘岗位收集 - {date_str}")
    print(f"   关键词: {keywords}")
    print(f"   地点:   {location or '不限'}")
    print()

    # 收集所有岗位
    all_jobs: list[Job] = []
    sources = config.get("sources", {})

    source_fns = [
        ("Indeed", lambda: fetch_indeed(keywords, location, max_results)),
        ("RemoteOK", lambda: fetch_remoteok(keywords, max_results)),
        ("Jobicy", lambda: fetch_jobicy(keywords, max_results)),
    ]
    total = sum(1 for name, _ in source_fns if sources.get(name.lower(), True))
    idx = 0
    for name, fn in source_fns:
        if not sources.get(name.lower(), True):
            continue
        idx += 1
        print(f"[{idx}/{total}] 正在搜索 {name}...")
        jobs = fn()
        all_jobs.extend(jobs)

    # 去重
    seen = set()
    unique_jobs = []
    for job in all_jobs:
        fp = job.fingerprint
        if fp not in seen:
            seen.add(fp)
            unique_jobs.append(job)

    print(f"\n📊 去重后共 {len(unique_jobs)} 个岗位 (原始 {len(all_jobs)} 个)")

    # 生成并保存日报
    markdown = render_markdown(unique_jobs, date_str)
    save_report(markdown, output_dir, date_str)


if __name__ == "__main__":
    main()
