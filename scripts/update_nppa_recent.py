#!/usr/bin/env python3
"""Append recent NPPA approval records to the existing full official dataset and merge changes.
Run on a copy; never silently replace the historical data if official validation fails.
"""
import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
spec=importlib.util.spec_from_file_location("nppa_fetch",REPO/"scripts/fetch_nppa.py")
mod=importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

SOURCE_LINKS = (
    ("国产", "2026年9月国产游戏", "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/202609/t20260930_1011281.html", 206),
    ("国产", "2026年10月国产游戏", "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/202610/t20261029_1015044.html", 183),
    ("进口", "2026年进口游戏年度名单", "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/jkwlyxspxx/202601/t20260126_947910.html", 58),
)
CHANGE_URL = "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/yxspbgxx/202601/t20260126_947941.html"
EXPECTED_CHANGE_COUNT = 97
DATA = REPO/"public/nppa-games.json"
CSV = REPO/"data/nppa-games.csv"

def valid(s):
    return isinstance(s,str) and s.strip() and s.strip()!="—"

def doc_key(row):
    value = mod.normalize_document_number(str(row.get("documentNumber","")))
    return value if valid(value) else ""

def isbn_key(row):
    value = mod.normalize_isbn(str(row.get("isbn","")))
    return value if valid(value) else ""

def main():
    a=argparse.ArgumentParser()
    a.add_argument("--apply",action="store_true")
    args=a.parse_args()

    baseline=json.loads(DATA.read_text(encoding="utf-8"))
    original_records=baseline["records"]
    records=list(original_records)
    old_count=len(records)
    last_approval=max((x.get("recordDate") or x.get("approvalDate") or "") for x in records)
    client=mod.PoliteClient(0.6)

    by_doc=defaultdict(list)
    by_isbn=defaultdict(list)
    for r in records:
        if doc_key(r): by_doc[doc_key(r)].append(r)
        if isbn_key(r): by_isbn[isbn_key(r)].append(r)

    fetched_counts={}
    additions=[]
    enrichments=0
    for typ,title,url,expected in SOURCE_LINKS:
        detail=mod.DetailLink(typ,2026,title,url)
        fetched=mod.parse_approval_page(client.get_soup(url),detail)
        if len(fetched)!=expected:
            raise RuntimeError(f"{title}: expected {expected} items but got {len(fetched)}; not modifying dataset")
        fetched_counts[title]=len(fetched)
        for item in fetched:
            doc=doc_key(item); isbn=isbn_key(item)
            found=by_doc.get(doc,[]) if doc else []
            if not found and isbn: found=by_isbn.get(isbn,[])
            if found:
                for existing in found:
                    if existing.get("sourceType") in (None,"未标明","—"):
                        existing["sourceType"]=typ;enrichments+=1
                    if not valid(existing.get("category")) and valid(item.get("category")):
                        existing["category"]=item["category"]
                    if not valid(existing.get("archiveSourceUrl")) and existing.get("sourceUrl")!=url:
                        existing["archiveSourceUrl"]=url
                    if not valid(existing.get("approvalDate")):
                        existing["approvalDate"]=item["approvalDate"]
            else:
                item["recordDate"]=item["approvalDate"]
                records.append(item)
                additions.append(item)
                if doc: by_doc[doc].append(item)
                if isbn: by_isbn[isbn].append(item)

    # A 2005-era title can enter the current public search registry after a 2026 approval change.
    # Recent-query records missing from the old snapshot or monthly approvals must also be included.
    recovered_from_official_search=[]
    for page in range(1,10):
        result=client.post_search(page,100)
        for item in result.get("data",[]):
            recent=mod.parse_search_record(item)
            if not recent: continue
            if recent["recordDate"] < last_approval: continue
            d=doc_key(recent); isbn=isbn_key(recent)
            if not d or d in by_doc or (isbn and isbn in by_isbn): continue
            records.append(recent)
            by_doc[d].append(recent)
            if isbn: by_isbn[isbn].append(recent)
            recovered_from_official_search.append(recent)

    change_link=mod.DetailLink("变更",2026,"2026年游戏审批变更信息",CHANGE_URL)
    changes=mod.parse_change_page(client.get_soup(CHANGE_URL),change_link)
    if len(changes)!=EXPECTED_CHANGE_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_CHANGE_COUNT} annual change rows, got {len(changes)}; abort")

    changed_docs=set();unmatched_changes=[]
    for change in sorted(changes,key=lambda x:(x["changeDate"],x["name"])):
        doc=doc_key(change)
        found=by_doc.get(doc,[])
        if not found:
            unmatched_changes.append({"name":change["name"],"document":doc})
            continue
        for r in found:
            if r.get("changeDate","") > change["changeDate"]:
                continue
            old_name=r["name"]
            if mod.normalize_name(old_name) != mod.normalize_name(change["name"]):
                aliases=list(r.get("aliases") or [])
                aliases.extend([old_name,*change.get("aliases",[])])
                r["aliases"]=list(dict.fromkeys(x for x in aliases if mod.normalize_name(x)!=mod.normalize_name(change["name"])))
                r["name"]=change["name"]
            else:
                aliases=list(r.get("aliases") or [])+list(change.get("aliases",[]))
                r["aliases"]=list(dict.fromkeys(x for x in aliases if mod.normalize_name(x)!=mod.normalize_name(r["name"])))
            for field in ("category","publisher","operator"):
                if valid(change.get(field)):
                    r[field]=change[field]
            r["changeInfo"]=change["changeInfo"]
            r["changeDate"]=change["changeDate"]
            r["changeSourceUrl"]=change["sourceUrl"]
            changed_docs.add(doc)

    # Match historical records by *approval document*, not names; retain older names as aliases.
    total=len(records)
    official_api=client.post_search(1,100)
    official_total=int(official_api.get("total",0))
    if official_total < old_count:
        raise RuntimeError(f"Official total dropped unexpectedly: {official_total} < {old_count}")
    if total != official_total:
        print("RECONCILIATION DEBUG: Local count", total, "vs official", official_total, flush=True)
        missing=[]
        for page in range(1,10):
            current=client.post_search(page,100)
            for item in current.get("data",[]):
                record=mod.parse_search_record(item)
                if not record: continue
                d=doc_key(record)
                if d and d not in by_doc:
                    missing.append((page,record.get("name"),d,record.get("recordDate"),record.get("sourceUrl")))
        print("MISSING_IN_APPROVALS_AND_BASE:",json.dumps(missing,ensure_ascii=False),flush=True)
        raise RuntimeError(f"Counts differ: combined {total} != official {official_total}; source or reconciliation incomplete. No files changed.")

    records.sort(key=lambda r:(r.get("recordDate") or r.get("approvalDate") or "",r["name"]),reverse=True)
    metadata=dict(baseline["metadata"])
    metadata.update({
        "source":"国家新闻出版署游戏审批结果查询库及月度审批公示",
        "fetchedAt":datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updatedThrough":max((r.get("recordDate") or r.get("approvalDate") or "") for r in records),
        "recordCount":total,
        "officialSearchTotal":official_total,
        "incrementalBaseCount":old_count,
        "incrementalAdded":len(additions)+len(recovered_from_official_search),
        "incrementalOfficialRegistryRecovered":len(recovered_from_official_search),
        "incrementalApprovedBatchCounts":fetched_counts,
        "incrementalMergedChangedDocuments":len(changed_docs),
        "annualChangeRows":len(changes),
        "incrementalMethod":"Approval-document matching + annual change merge; pre-existing records retained.",
        "incrementalSources":[u for _,_,u,_ in SOURCE_LINKS]+[CHANGE_URL],
    })
    payload={"metadata":metadata,"records":records}

    must_have={
        "傲视怒斩":"国新出审〔2026〕2510号",
        "暗夜勇士":"国新出审〔2026〕2218号",
        "矮人军团自走棋":"国新出审〔2026〕2693号",
    }
    for name,doc in must_have.items():
        assert any(r["name"]==name and doc_key(r)==doc for r in records),f"Verification failed {name}"
    report={
        "oldCount":old_count,
        "newCount":total,
        "officialTotal":official_total,
        "added":len(additions)+len(recovered_from_official_search),
        "approvalPageAdded":len(additions),
        "officialRegistryRecovered":len(recovered_from_official_search),
        "recoveredSample":[{"name":x["name"],"documentNumber":x["documentNumber"]} for x in recovered_from_official_search[:5]],
        "enrichedExisting":enrichments,
        "yearChangeRows":len(changes),
        "affectedDocumentsByChanges":len(changed_docs),
        "unmatchedChanges":unmatched_changes,
        "latest":metadata["updatedThrough"],
        "addedSample":[{"name":x["name"],"documentNumber":x["documentNumber"]} for x in additions[:5]],
    }
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    (REPO/"nppa-sync-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if args.apply:
        mod.write_json(DATA,payload)
        mod.write_csv(CSV,records)
        print(f"APPLIED JSON={DATA.stat().st_size} CSV={CSV.stat().st_size}",flush=True)
    else:
        print("DRY-RUN: no existing data files modified",flush=True)

if __name__=="__main__":
    main()

