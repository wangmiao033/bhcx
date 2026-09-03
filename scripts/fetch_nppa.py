#!/usr/bin/env python3
"""低频抓取国家新闻出版署网游审批公示，生成网站可直接使用的数据文件。"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


SOURCES = {
    "国产": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/index.html",
    "进口": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/jkwlyxspxx/index.html",
    "变更": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/yxspbgxx/index.html",
}

DETAIL_TITLE_PATTERNS = {
    "国产": re.compile(r"(20\d{2})年.*国产网络游戏审批信息"),
    "进口": re.compile(r"(20\d{2})年.*进口网络游戏审批信息"),
    "变更": re.compile(r"(20\d{2})年.*游戏审批变更信息"),
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\xa0", " ").strip())


def clean_company(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ").strip()) or "—"


def normalize_document_number(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("﹝", "〔").replace("﹞", "〕")


def normalize_date(value: str) -> str:
    match = re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", value)
    if not match:
        return ""
    year, month, day = map(int, match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def normalize_name(value: str) -> str:
    return re.sub(r"[《》·\s:：—_\-]", "", value).lower()


@dataclass
class DetailLink:
    source_type: str
    year: int
    title: str
    url: str


class PoliteClient:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self.last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36 "
                    "personal-research-nppa-indexer/1.0"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    def get_soup(self, url: str) -> BeautifulSoup:
        elapsed = time.monotonic() - self.last_request_at
        if self.last_request_at and elapsed < self.interval:
            time.sleep(self.interval - elapsed)

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=35)
                self.last_request_at = time.monotonic()
                response.raise_for_status()
                return BeautifulSoup(response.content, "html.parser", from_encoding="utf-8")
            except (requests.RequestException, OSError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"请求失败：{url} ({last_error})")


def page_count(soup: BeautifulSoup) -> int:
    source = str(soup)
    match = re.search(r"var\s+_pageCount\s*=\s*(\d+)", source)
    return int(match.group(1)) if match else 1


def list_page_url(index_url: str, page: int) -> str:
    if page == 0:
        return index_url
    return index_url.rsplit("/", 1)[0] + f"/index_{page}.html"


def discover_details(client: PoliteClient, source_type: str, start_year: int) -> list[DetailLink]:
    index_url = SOURCES[source_type]
    first_soup = client.get_soup(index_url)
    count = page_count(first_soup)
    found: dict[str, DetailLink] = {}
    old_page_streak = 0

    for page in range(count):
        soup = first_soup if page == 0 else client.get_soup(list_page_url(index_url, page))
        page_years: list[int] = []
        candidates = [
            (anchor.get_text(" ", strip=True), anchor.get("href", ""))
            for anchor in soup.select("a[href]")
        ]
        # “游戏审批变更信息”的年份链接写在页面脚本字符串中，不会成为实际 DOM 节点。
        candidates.extend(
            (title, href)
            for href, title in re.findall(
                r'<a\s+href="([^"]+\.html)"[^>]*>\s*([^<]+?)\s*</a>',
                str(soup),
                flags=re.IGNORECASE,
            )
        )
        for raw_title, href in candidates:
            title = clean_company(raw_title)
            match = DETAIL_TITLE_PATTERNS[source_type].search(title)
            if not match:
                continue
            year = int(match.group(1))
            page_years.append(year)
            if year < start_year:
                continue
            url = urljoin(index_url, href)
            if not re.search(r"/t\d+_\d+\.html$", url):
                continue
            found[url] = DetailLink(source_type, year, title, url)

        if page_years and max(page_years) < start_year:
            old_page_streak += 1
            if old_page_streak >= 2:
                break
        else:
            old_page_streak = 0

    return sorted(found.values(), key=lambda item: (item.year, item.url), reverse=True)


def row_cells(row: Any) -> list[str]:
    return [clean_company(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]


def parse_approval_page(soup: BeautifulSoup, link: DetailLink) -> list[dict[str, Any]]:
    table = soup.select_one("table")
    if not table:
        raise ValueError(f"页面未找到审批表格：{link.url}")

    records: list[dict[str, Any]] = []
    for row in table.select("tr")[1:]:
        cells = row_cells(row)
        if len(cells) < 7 or not cells[0].isdigit():
            continue

        # 近年页面表头仍含“申报类别”，但数据行实际省略了该列。
        if len(cells) >= 8:
            _, name, category, publisher, operator, document_number, isbn, approval_date = cells[:8]
        else:
            _, name, publisher, operator, document_number, isbn, approval_date = cells[:7]
            category = "—"

        date = normalize_date(approval_date)
        if not name or not date:
            continue
        records.append(
            {
                "name": name,
                "aliases": [],
                "category": category,
                "publisher": publisher,
                "operator": operator,
                "documentNumber": normalize_document_number(document_number),
                "isbn": clean_text(isbn),
                "approvalDate": date,
                "sourceType": link.source_type,
                "sourceUrl": link.url,
            }
        )
    return records


def parse_change_page(soup: BeautifulSoup, link: DetailLink) -> list[dict[str, Any]]:
    table = soup.select_one("table")
    if not table:
        raise ValueError(f"页面未找到变更表格：{link.url}")

    changes: list[dict[str, Any]] = []
    for row in table.select("tr")[1:]:
        cells = row_cells(row)
        if len(cells) < 8 or not cells[0].isdigit():
            continue
        _, name, category, publisher, operator, change_info, document_number, change_date = cells[:8]
        date = normalize_date(change_date)
        if not name or not date:
            continue
        aliases = re.findall(r"原名[为：:]?《([^》]+)》", change_info)
        changes.append(
            {
                "name": name,
                "aliases": list(dict.fromkeys(aliases)),
                "category": category,
                "publisher": publisher,
                "operator": operator,
                "documentNumber": normalize_document_number(document_number),
                "changeDate": date,
                "changeInfo": change_info,
                "sourceUrl": link.url,
            }
        )
    return changes


def deduplicate(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record["documentNumber"] or record["isbn"] or (
            f"{normalize_name(record['name'])}|{record['publisher']}|{record['approvalDate']}"
        )
        existing = unique.get(key)
        if existing is None or record["approvalDate"] > existing["approvalDate"]:
            unique[key] = record
    return list(unique.values())


def merge_changes(records: list[dict[str, Any]], changes: list[dict[str, Any]]) -> int:
    by_document = {
        normalize_document_number(record["documentNumber"]): record
        for record in records
        if record.get("documentNumber")
    }
    merged = 0
    for change in sorted(changes, key=lambda item: item["changeDate"]):
        record = by_document.get(normalize_document_number(change["documentNumber"]))
        if not record:
            continue

        previous_name = record["name"]
        explicit_aliases = change.get("aliases", [])
        if explicit_aliases and normalize_name(previous_name) in {normalize_name(alias) for alias in explicit_aliases}:
            record["name"] = change["name"]
        elif normalize_name(previous_name) != normalize_name(change["name"]):
            explicit_aliases = [previous_name, *explicit_aliases]
            record["name"] = change["name"]

        aliases = [*record.get("aliases", []), *explicit_aliases]
        record["aliases"] = list(
            dict.fromkeys(alias for alias in aliases if normalize_name(alias) != normalize_name(record["name"]))
        )
        for field in ("category", "publisher", "operator"):
            if change.get(field) and change[field] != "—":
                record[field] = change[field]
        record["changeInfo"] = change["changeInfo"]
        record["changeDate"] = change["changeDate"]
        record["changeSourceUrl"] = change["sourceUrl"]
        merged += 1
    return merged


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "name",
        "aliases",
        "category",
        "publisher",
        "operator",
        "documentNumber",
        "isbn",
        "approvalDate",
        "sourceType",
        "changeDate",
        "changeInfo",
        "sourceUrl",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["aliases"] = "、".join(record.get("aliases", []))
            writer.writerow(row)


def build_dataset(args: argparse.Namespace) -> dict[str, Any]:
    client = PoliteClient(args.interval)
    approval_links: list[DetailLink] = []
    for source_type in ("国产", "进口"):
        print(f"[发现] 正在读取{source_type}公示目录…", flush=True)
        links = discover_details(client, source_type, args.start_year)
        print(f"[发现] {source_type}详情页 {len(links)} 个", flush=True)
        approval_links.extend(links)

    change_links: list[DetailLink] = []
    if not args.skip_changes:
        print("[发现] 正在读取审批变更目录…", flush=True)
        change_links = discover_details(client, "变更", args.start_year)
        print(f"[发现] 变更详情页 {len(change_links)} 个", flush=True)

    records: list[dict[str, Any]] = []
    total_pages = len(approval_links)
    for index, link in enumerate(approval_links, 1):
        soup = client.get_soup(link.url)
        page_records = parse_approval_page(soup, link)
        records.extend(page_records)
        print(f"[审批 {index}/{total_pages}] {link.title}：{len(page_records)} 条", flush=True)

    changes: list[dict[str, Any]] = []
    for index, link in enumerate(change_links, 1):
        soup = client.get_soup(link.url)
        page_changes = parse_change_page(soup, link)
        changes.extend(page_changes)
        print(f"[变更 {index}/{len(change_links)}] {link.title}：{len(page_changes)} 条", flush=True)

    records = deduplicate(records)
    merged_changes = merge_changes(records, changes)
    records.sort(key=lambda item: (item["approvalDate"], item["name"]), reverse=True)
    newest_date = max((record["approvalDate"] for record in records), default="")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return {
        "metadata": {
            "source": "国家新闻出版署网络游戏审批公示",
            "startYear": args.start_year,
            "fetchedAt": fetched_at,
            "updatedThrough": newest_date,
            "recordCount": len(records),
            "changeEntryCount": len(changes),
            "mergedChangeCount": merged_changes,
            "sources": SOURCES,
        },
        "records": records,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取国家新闻出版署网游审批公示并生成 JSON / CSV")
    parser.add_argument("--start-year", type=int, default=2014, help="最早抓取年份，默认 2014")
    parser.add_argument("--interval", type=float, default=2.2, help="相邻请求最小间隔秒数，默认 2.2")
    parser.add_argument("--output", type=Path, default=Path("public/nppa-games.json"), help="JSON 输出路径")
    parser.add_argument("--csv-output", type=Path, default=Path("data/nppa-games.csv"), help="CSV 输出路径")
    parser.add_argument("--skip-changes", action="store_true", help="不合并审批变更信息")
    parser.add_argument("--changes-only", action="store_true", help="只抓取变更并合并到已有 JSON 数据")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_year < 2000 or args.start_year > datetime.now().year + 1:
        print("start-year 不在合理范围内", file=sys.stderr)
        return 2
    if args.changes_only:
        if not args.output.exists():
            print(f"找不到已有数据文件：{args.output}", file=sys.stderr)
            return 2
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        records = payload.get("records", [])
        client = PoliteClient(args.interval)
        links = discover_details(client, "变更", args.start_year)
        print(f"[发现] 变更详情页 {len(links)} 个", flush=True)
        changes: list[dict[str, Any]] = []
        for index, link in enumerate(links, 1):
            page_changes = parse_change_page(client.get_soup(link.url), link)
            changes.extend(page_changes)
            print(f"[变更 {index}/{len(links)}] {link.title}：{len(page_changes)} 条", flush=True)
        merged = merge_changes(records, changes)
        payload.setdefault("metadata", {}).update(
            {
                "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "changeEntryCount": len(changes),
                "mergedChangeCount": merged,
            }
        )
    else:
        payload = build_dataset(args)
    write_json(args.output, payload)
    write_csv(args.csv_output, payload["records"])
    metadata = payload["metadata"]
    print(
        f"[完成] {metadata['recordCount']} 条审批记录，合并 {metadata['mergedChangeCount']} 条变更；"
        f"最新日期 {metadata['updatedThrough']}",
        flush=True,
    )
    print(f"[输出] {args.output.resolve()}", flush=True)
    print(f"[输出] {args.csv_output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
