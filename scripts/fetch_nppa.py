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
    "总目录": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/index.html",
    "国产": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/index.html",
    "进口": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/jkwlyxspxx/index.html",
    "变更": "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/yxspbgxx/index.html",
    "查询库": "https://www.nppa.gov.cn/bsfw/jggs/cxjg/index.html",
}

SEARCH_API_URL = "https://www.nppa.gov.cn/was5/web/search?channelid=288556"

MASTER_APPROVAL_PATTERN = re.compile(
    r"(20\d{2})年.*?(国产网络游戏|进口网络游戏|进口电子游戏)审批信息"
)
MASTER_CHANGE_PATTERN = re.compile(r"(20\d{2})年.*?游戏审批变更信息")

DETAIL_TITLE_PATTERNS = {
    "国产": re.compile(r"(20\d{2})年.*国产网络游戏审批信息"),
    "进口": re.compile(r"(20\d{2})年.*进口网络游戏审批信息"),
    "变更": re.compile(r"(20\d{2})年.*游戏审批变更信息"),
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("\xa0", " ").strip())


def normalize_isbn(value: str) -> str:
    compact = clean_text(value)
    if not compact:
        return "—"
    if compact[:4].lower() == "isbn" and len(compact) > 4:
        return f"ISBN {compact[4:]}"
    return compact


def clean_company(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ").strip()) or "—"


def normalize_document_number(value: str) -> str:
    bracket_map = str.maketrans(
        {
            "[": "〔",
            "]": "〕",
            "﹝": "〔",
            "﹞": "〕",
            "【": "〔",
            "】": "〕",
        }
    )
    return re.sub(r"\s+", "", value).translate(bracket_map)


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

    def post_search(self, page: int, size: int = 100) -> dict[str, Any]:
        elapsed = time.monotonic() - self.last_request_at
        if self.last_request_at and elapsed < self.interval:
            time.sleep(self.interval - elapsed)

        payload = {
            "page": page,
            "size": size,
            "mc": "",
            "cbdw": "",
            "yydw": "",
            "description": "",
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.post(
                    SEARCH_API_URL,
                    data=payload,
                    headers={"Referer": SOURCES["查询库"]},
                    timeout=45,
                )
                self.last_request_at = time.monotonic()
                response.raise_for_status()
                raw = response.content.decode("utf-8-sig", errors="replace")
                start = raw.find("{")
                end = raw.rfind("}")
                if start < 0 or end < start:
                    raise ValueError("响应中未找到 JSON")
                result = json.loads(raw[start : end + 1])
                if not isinstance(result.get("data"), list):
                    raise ValueError("响应缺少 data 列表")
                return result
            except (requests.RequestException, OSError, ValueError, json.JSONDecodeError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"查询库第 {page} 页请求失败：{last_error}")


def page_count(soup: BeautifulSoup) -> int:
    source = str(soup)
    match = re.search(r"var\s+_pageCount\s*=\s*(\d+)", source)
    return int(match.group(1)) if match else 1


def list_page_url(index_url: str, page: int) -> str:
    if page == 0:
        return index_url
    return index_url.rsplit("/", 1)[0] + f"/index_{page}.html"


def listing_candidates(soup: BeautifulSoup) -> list[tuple[str, str]]:
    candidates = [
        (anchor.get_text(" ", strip=True), anchor.get("href", ""))
        for anchor in soup.select("a[href]")
    ]
    # 部分目录把链接写在 document.write / JavaScript 字符串中。
    candidates.extend(
        (title, href)
        for href, title in re.findall(
            r'<a\s+href="([^"]+\.html)"[^>]*>\s*([^<]+?)\s*</a>',
            str(soup),
            flags=re.IGNORECASE,
        )
    )
    return list(dict.fromkeys((clean_company(title), href) for title, href in candidates))


def discover_all_details(
    client: PoliteClient, start_year: int
) -> tuple[list[DetailLink], list[DetailLink]]:
    """从游戏审批结果总目录发现国产、进口、进口电子游戏和变更详情页。"""
    index_url = SOURCES["总目录"]
    first_soup = client.get_soup(index_url)
    count = page_count(first_soup)
    approvals: dict[str, DetailLink] = {}
    changes: dict[str, DetailLink] = {}

    for page in range(count):
        soup = first_soup if page == 0 else client.get_soup(list_page_url(index_url, page))
        for title, href in listing_candidates(soup):
            approval_match = MASTER_APPROVAL_PATTERN.search(title)
            change_match = MASTER_CHANGE_PATTERN.search(title)
            if not approval_match and not change_match:
                continue
            year = int((approval_match or change_match).group(1))
            if year < start_year:
                continue
            url = urljoin(index_url, href)
            if not re.search(r"/t\d+_\d+\.html$", url):
                continue
            if approval_match:
                source_type = "国产" if "国产网络游戏" in approval_match.group(2) else "进口"
                approvals[url] = DetailLink(source_type, year, title, url)
            else:
                changes[url] = DetailLink("变更", year, title, url)

    return (
        sorted(approvals.values(), key=lambda item: (item.year, item.url), reverse=True),
        sorted(changes.values(), key=lambda item: (item.year, item.url), reverse=True),
    )


def discover_details(client: PoliteClient, source_type: str, start_year: int) -> list[DetailLink]:
    index_url = SOURCES[source_type]
    first_soup = client.get_soup(index_url)
    count = page_count(first_soup)
    found: dict[str, DetailLink] = {}
    old_page_streak = 0

    for page in range(count):
        soup = first_soup if page == 0 else client.get_soup(list_page_url(index_url, page))
        page_years: list[int] = []
        for title, href in listing_candidates(soup):
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
        if len(cells) < 5 or not cells[0].isdigit():
            continue

        # 近年页面表头仍含“申报类别”，但数据行实际省略了该列。
        if len(cells) >= 8:
            _, name, category, publisher, operator, document_number, isbn, approval_date = cells[:8]
        elif len(cells) == 7:
            _, name, publisher, operator, document_number, isbn, approval_date = cells[:7]
            category = "—"
        elif len(cells) == 5:
            _, name, publisher, document_number, approval_date = cells
            category = "进口电子游戏" if "进口电子游戏" in link.title else "—"
            operator = "—"
            isbn = "—"
        else:
            continue

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
                "isbn": normalize_isbn(isbn),
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


def parse_search_record(item: dict[str, Any]) -> dict[str, Any] | None:
    """把官网“查询结果库”的当前记录转换为站点字段。"""
    name = clean_company(str(item.get("mc", "")))
    record_date = normalize_date(str(item.get("sj", "")))
    if name == "—" or not record_date:
        return None

    source_url = str(item.get("docpuburl", "")).strip()
    return {
        "name": name,
        "aliases": [],
        "category": "—",
        "publisher": clean_company(str(item.get("cbdw", ""))),
        "operator": clean_company(str(item.get("yydw", ""))),
        "documentNumber": normalize_document_number(str(item.get("wh", ""))) or "—",
        "isbn": normalize_isbn(str(item.get("cbwh", ""))),
        # 未匹配到历史公示时，以官网当前记录日期作为可用的批准日期。
        "approvalDate": record_date,
        "recordDate": record_date,
        "sourceType": "未标明",
        "sourceUrl": source_url or SOURCES["查询库"],
    }


def fetch_search_database(client: PoliteClient, page_size: int = 100) -> tuple[list[dict[str, Any]], int]:
    """低频分页抓取官网当前查询结果库。"""
    first = client.post_search(1, page_size)
    total = int(first.get("total", 0))
    pages = max(1, (total + page_size - 1) // page_size)
    records: list[dict[str, Any]] = []

    for page in range(1, pages + 1):
        result = first if page == 1 else client.post_search(page, page_size)
        expected_count = page_size if page < pages else total - page_size * (pages - 1)
        for retry in range(3):
            if len(result.get("data", [])) == expected_count:
                break
            print(
                f"[查询库 {page}/{pages}] 本页仅返回 {len(result.get('data', []))}/"
                f"{expected_count} 条，正在重试 {retry + 1}/3…",
                flush=True,
            )
            result = client.post_search(page, page_size)
        if len(result.get("data", [])) != expected_count:
            raise RuntimeError(
                f"查询库第 {page} 页数量异常：应有 {expected_count} 条，"
                f"实际 {len(result.get('data', []))} 条"
            )
        page_records = [
            record
            for item in result.get("data", [])
            if isinstance(item, dict)
            for record in [parse_search_record(item)]
            if record is not None
        ]
        records.extend(page_records)
        if page == 1 or page % 10 == 0 or page == pages:
            print(
                f"[查询库 {page}/{pages}] 已读取 {len(records):,}/{total:,} 条",
                flush=True,
            )
    return records, total


def usable_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip() != "—"


def merge_search_database(
    current_records: list[dict[str, Any]], historical_records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    """以当前查询库为主，并从月度归档补回首次批准、类别和变更前信息。"""
    history_by_document: dict[str, dict[str, Any]] = {}
    history_by_isbn: dict[str, dict[str, Any]] = {}
    for record in historical_records:
        document = normalize_document_number(str(record.get("documentNumber", "")))
        isbn = normalize_isbn(str(record.get("isbn", "")))
        if document and document != "—":
            history_by_document[document] = record
        if isbn and isbn != "—":
            history_by_isbn[isbn] = record

    merged: list[dict[str, Any]] = []
    matched_count = 0
    synthesized_change_count = 0
    for current in current_records:
        document = normalize_document_number(str(current.get("documentNumber", "")))
        isbn = normalize_isbn(str(current.get("isbn", "")))
        historical = history_by_document.get(document)
        if historical is None and isbn and isbn != "—":
            historical = history_by_isbn.get(isbn)

        if historical is None:
            merged.append(current)
            continue

        matched_count += 1
        record = dict(current)
        record["approvalDate"] = historical.get("approvalDate") or current["recordDate"]
        record["sourceType"] = historical.get("sourceType") or "未标明"
        record["category"] = historical.get("category") or "—"
        record["aliases"] = list(historical.get("aliases") or [])

        archive_url = historical.get("archiveSourceUrl") or historical.get("sourceUrl")
        if usable_value(archive_url) and archive_url != record.get("sourceUrl"):
            record["archiveSourceUrl"] = archive_url

        differences: list[str] = []
        comparison_fields = (
            ("name", "原游戏名称"),
            ("publisher", "原出版单位"),
            ("operator", "原运营单位"),
        )
        for field, label in comparison_fields:
            old_value = historical.get(field)
            new_value = record.get(field)
            if usable_value(old_value) and usable_value(new_value) and old_value != new_value:
                differences.append(f"{label}（{old_value}）")
                if field == "name":
                    record["aliases"] = list(
                        dict.fromkeys([*record["aliases"], str(old_value)])
                    )

        if differences:
            record["changeInfo"] = "；".join(differences)
            record["changeDate"] = record["recordDate"]
            synthesized_change_count += 1
        else:
            for field in ("changeInfo", "changeDate", "changeSourceUrl"):
                if historical.get(field):
                    record[field] = historical[field]

        record["aliases"] = [
            alias
            for alias in dict.fromkeys(record["aliases"])
            if normalize_name(alias) != normalize_name(record["name"])
        ]
        merged.append(record)

    # 官网 total 统计的是公开结果行数；即使文号重复也保留，确保数据量一致。
    return merged, matched_count, synthesized_change_count


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
        "recordDate",
        "sourceType",
        "changeDate",
        "changeInfo",
        "sourceUrl",
        "archiveSourceUrl",
        "changeSourceUrl",
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
    print("[发现] 正在读取游戏审批结果总目录…", flush=True)
    master_approvals, master_changes = discover_all_details(client, args.start_year)

    # 总目录保留了 2009 年起的旧记录，但最新月份可能晚于分类目录同步。
    # 两类目录取并集，避免只抓总目录时漏掉最近发布的批次。
    approval_map = {link.url: link for link in master_approvals}
    for source_type in ("国产", "进口"):
        print(f"[发现] 正在核对{source_type}分类目录…", flush=True)
        for link in discover_details(client, source_type, args.start_year):
            approval_map[link.url] = link
    approval_links = sorted(
        approval_map.values(), key=lambda item: (item.year, item.url), reverse=True
    )

    change_map = {link.url: link for link in master_changes}
    if not args.skip_changes:
        for link in discover_details(client, "变更", args.start_year):
            change_map[link.url] = link
    change_links = [] if args.skip_changes else sorted(
        change_map.values(), key=lambda item: (item.year, item.url), reverse=True
    )
    print(
        f"[发现] 审批详情页 {len(approval_links)} 个，变更详情页 {len(change_links)} 个",
        flush=True,
    )

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


def build_search_dataset(
    args: argparse.Namespace, historical_payload: dict[str, Any]
) -> dict[str, Any]:
    """构建官网当前全量查询库，并用已有月度归档补充历史字段。"""
    client = PoliteClient(args.interval)
    print("[查询库] 正在读取国家新闻出版署当前全量结果…", flush=True)
    current_records, official_total = fetch_search_database(client)
    historical_records = historical_payload.get("records", [])
    records, matched_count, synthesized_change_count = merge_search_database(
        current_records, historical_records
    )
    records.sort(
        key=lambda item: (item.get("recordDate") or item["approvalDate"], item["name"]),
        reverse=True,
    )
    newest_date = max(
        (record.get("recordDate") or record["approvalDate"] for record in records),
        default="",
    )
    old_metadata = historical_payload.get("metadata", {})
    return {
        "metadata": {
            "source": "国家新闻出版署游戏审批结果查询库",
            "startYear": args.start_year,
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updatedThrough": newest_date,
            "recordCount": len(records),
            "officialSearchTotal": official_total,
            "historicalRecordCount": len(historical_records),
            "matchedHistoricalCount": matched_count,
            "synthesizedChangeCount": synthesized_change_count,
            "changeEntryCount": old_metadata.get("changeEntryCount", 0),
            "mergedChangeCount": old_metadata.get("mergedChangeCount", 0),
            "sources": SOURCES,
        },
        "records": records,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取国家新闻出版署网游审批公示并生成 JSON / CSV")
    parser.add_argument("--start-year", type=int, default=2009, help="最早抓取年份，默认 2009")
    parser.add_argument("--interval", type=float, default=2.2, help="相邻请求最小间隔秒数，默认 2.2")
    parser.add_argument("--output", type=Path, default=Path("public/nppa-games.json"), help="JSON 输出路径")
    parser.add_argument("--csv-output", type=Path, default=Path("data/nppa-games.csv"), help="CSV 输出路径")
    parser.add_argument("--skip-changes", action="store_true", help="不合并审批变更信息")
    parser.add_argument("--changes-only", action="store_true", help="只抓取变更并合并到已有 JSON 数据")
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="只刷新官网当前查询库，并用已有 JSON 补充历史字段",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_year < 2000 or args.start_year > datetime.now().year + 1:
        print("start-year 不在合理范围内", file=sys.stderr)
        return 2
    if args.changes_only and args.search_only:
        print("changes-only 与 search-only 不能同时使用", file=sys.stderr)
        return 2
    if args.search_only:
        if not args.output.exists():
            print(f"找不到用于补充历史字段的已有数据文件：{args.output}", file=sys.stderr)
            return 2
        historical_payload = json.loads(args.output.read_text(encoding="utf-8"))
        payload = build_search_dataset(args, historical_payload)
    elif args.changes_only:
        if not args.output.exists():
            print(f"找不到已有数据文件：{args.output}", file=sys.stderr)
            return 2
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        records = payload.get("records", [])
        client = PoliteClient(args.interval)
        _, master_links = discover_all_details(client, args.start_year)
        link_map = {link.url: link for link in master_links}
        for link in discover_details(client, "变更", args.start_year):
            link_map[link.url] = link
        links = sorted(
            link_map.values(), key=lambda item: (item.year, item.url), reverse=True
        )
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
        historical_payload = build_dataset(args)
        payload = build_search_dataset(args, historical_payload)
    write_json(args.output, payload)
    write_csv(args.csv_output, payload["records"])
    metadata = payload["metadata"]
    print(
        f"[完成] {metadata['recordCount']} 条审批记录，匹配历史 "
        f"{metadata.get('matchedHistoricalCount', 0)} 条；"
        f"最新日期 {metadata['updatedThrough']}",
        flush=True,
    )
    print(f"[输出] {args.output.resolve()}", flush=True)
    print(f"[输出] {args.csv_output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
