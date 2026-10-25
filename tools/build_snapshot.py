#!/usr/bin/env python3
"""Collect current project state into data/snapshot.json.

Run locally (where the live data lives), then commit/push the snapshot:
    python tools/build_snapshot.py

The Streamlit app (app.py) reads ONLY data/snapshot.json, so it runs
unchanged locally and on Streamlit Community Cloud. Hand-maintained facts
(platform status, mortgage) live in status.json at the repo root — edit
that file, then re-run this script.

Data sources (all read-only):
  * Shared DuckDB  ~/workspace/cadb2b-run/data/cbintel.duckdb
    (override with env MUNI_DB_PATH or CADB2B_DB_PATH)
  * Content engine ~/workspace/cadb2b-content/
  * Cron projections ~/workspace/cron.d/**, ~/workspace/goals/*/crons/**
  * status.json (this repo)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
WS = Path.home() / "workspace"
REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "data" / "snapshot.json"
STATUS_PATH = REPO_ROOT / "status.json"

SYSTEM_CRON_IDS = {"heartbeat", "deterministic-doctor", "profile-image",
                   "agentic-feature-tour"}
SYSTEM_CRON_PREFIXES = ("feed-pulse-",)


def iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.astimezone(JST).isoformat(timespec="seconds") if dt.tzinfo else dt.isoformat()
    return str(dt)


# ---------------------------------------------------------------- DuckDB ---
def collect_db() -> dict:
    import duckdb  # only needed by this script, not by app.py

    db_path = Path(
        os.environ.get("MUNI_DB_PATH")
        or os.environ.get("CADB2B_DB_PATH")
        or WS / "cadb2b-run" / "data" / "cbintel.duckdb"
    )
    out: dict = {"available": False}
    if not db_path.exists():
        out["error"] = f"DB not found: {db_path.name}"
        return out
    out["file_name"] = db_path.name
    out["size_mb"] = round(db_path.stat().st_size / 1e6, 1)
    out["mtime_jst"] = iso(datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc))

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {}
        for (tname,) in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall():
            try:
                tables[tname] = con.execute(f'SELECT count(*) FROM "{tname}"').fetchone()[0]
            except Exception:
                tables[tname] = None
        out["tables"] = tables

        fed: dict = {}
        if "fct_contract" in tables:
            fed["contracts"] = tables["fct_contract"]
            fed["goods_contracts"] = con.execute(
                "SELECT count(*) FROM fct_contract WHERE is_goods").fetchone()[0]
            fed["total_value_b_cad"] = round(
                con.execute("SELECT sum(value_cad) FROM fct_contract").fetchone()[0] / 1e9, 2)
        if "dim_supplier" in tables:
            fed["suppliers"] = tables["dim_supplier"]
        if "mart_supplier_leaderboard" in tables:
            fed["leaderboard_rows"] = tables["mart_supplier_leaderboard"]
            fed["leaderboard_segments"] = con.execute(
                "SELECT count(DISTINCT segment) FROM mart_supplier_leaderboard").fetchone()[0]
            seg_rows = con.execute(
                """SELECT segment_zh, segment_en, count(*) AS n_suppliers,
                          round(sum(value_cad)/1e6, 1) AS value_m_cad
                   FROM mart_supplier_segment
                   GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15""").fetchall()
            fed["top_segments"] = [
                {"segment_zh": r[0], "segment_en": r[1],
                 "n_suppliers": r[2], "value_m_cad": r[3]} for r in seg_rows]
        if "mart_open_goods_tenders" in tables:
            fed["open_goods_tenders"] = tables["mart_open_goods_tenders"]
            fed["open_goods_tenders_china_fit"] = con.execute(
                "SELECT count(*) FROM mart_open_goods_tenders WHERE china_fit").fetchone()[0]
        out["federal"] = fed

        muni: dict = {}
        if "stg_muni_tenders" in tables:
            muni["snapshot_rows"] = tables["stg_muni_tenders"]
            key = "coalesce(bid_number, detail_guid, '')"
            muni["tenders_deduped"] = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT source_portal, agency, {key} "
                "FROM stg_muni_tenders)").fetchone()[0]
            muni["goods_native_deduped"] = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT source_portal, agency, {key} "
                "FROM stg_muni_tenders WHERE bid_classification='Goods')").fetchone()[0]
            last = con.execute("SELECT max(scraped_at) FROM stg_muni_tenders").fetchone()[0]
            muni["last_scraped_at_jst"] = iso(last)
            muni["by_portal"] = [
                {"portal": r[0], "rows": r[1]} for r in con.execute(
                    "SELECT source_portal, count(*) FROM stg_muni_tenders "
                    "GROUP BY 1 ORDER BY 2 DESC").fetchall()]
        if "mart_muni_goods_leads" in tables:
            muni["leads_count"] = tables["mart_muni_goods_leads"]
            muni["leads_total_cad"] = round(con.execute(
                "SELECT coalesce(sum(value_cad),0) FROM mart_muni_goods_leads").fetchone()[0])
            lead_rows = con.execute(
                """SELECT buyer, title, supplier_raw, value_cad, segment_zh,
                          award_date, status
                   FROM mart_muni_goods_leads ORDER BY value_cad DESC""").fetchall()
            muni["leads"] = [
                {"buyer": r[0], "title": (r[1] or "").strip(),
                 "supplier": r[2], "value_cad": r[3],
                 "segment_zh": r[4], "award_date": iso(r[5]),
                 "status": r[6]} for r in lead_rows]
        out["municipal"] = muni
        out["available"] = True
    finally:
        con.close()
    return out


# ------------------------------------------------------------- content -----
def collect_content() -> dict:
    root = WS / "cadb2b-content"
    out: dict = {"available": root.exists()}
    if not root.exists():
        return out
    issues_dir = root / "issues"
    entries = []
    if issues_dir.exists():
        for p in sorted(issues_dir.iterdir()):
            if p.name.startswith("issue"):
                entries.append({
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "mtime_jst": iso(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)),
                })
    out["issues"] = entries
    out["issues_count"] = len(entries)
    out["latest_issue"] = entries[-1]["name"] if entries else None

    seen = root / "data" / "seen.json"
    if seen.exists():
        out["seen_file_kb"] = round(seen.stat().st_size / 1024)
        try:
            out["seen_entries"] = len(json.loads(seen.read_text(encoding="utf-8")))
        except Exception:
            out["seen_entries"] = None
    src = root / "config" / "sources.yaml"
    if src.exists():
        out["sources_count"] = len(re.findall(r"^\s+- name:", src.read_text(encoding="utf-8"), re.M))
    return out


# ----------------------------------------------------------------- crons ---
def _parse_frontmatter(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return None
    fm = m.group(1)
    job: dict = {}
    for field in ("id", "title", "owner"):
        mm = re.search(rf"^{field}:\s*(.+)$", fm, re.M)
        if mm:
            job[field] = mm.group(1).strip().strip("'\"")
    em = re.search(r"^enabled:\s*(\w+)", fm, re.M)
    job["enabled"] = (em.group(1) == "true") if em else True
    sm = re.search(r"schedule:\n((?:  .*\n?)+)", fm)
    sched: dict = {}
    if sm:
        block = sm.group(1)
        for field in ("kind", "time", "at"):
            mm = re.search(rf"^  {field}:\s*(.+)$", block, re.M)
            if mm:
                sched[field] = mm.group(1).strip()
        dm = re.search(r"^  dow:\s*\[(.+)\]", block, re.M)
        if dm:
            sched["dow"] = [d.strip() for d in dm.group(1).split(",")]
    job["schedule"] = sched
    return job


def _human_schedule(sched: dict) -> str:
    kind = sched.get("kind", "")
    t = sched.get("time", "")[:5]
    if kind == "weekly":
        dow = {"Mon": "周一", "Tue": "周二", "Wed": "周三", "Thu": "周四",
               "Fri": "周五", "Sat": "周六", "Sun": "周日"}
        days = "/".join(dow.get(d, d) for d in sched.get("dow", []))
        return f"每周{days} {t}".strip()
    if kind == "daily":
        return f"每天 {t}".strip()
    if kind == "runonce":
        return f"一次：{sched.get('at', '').replace('T', ' ')[:16]}"
    if kind == "interval":
        return "间隔运行"
    return kind or "—"


def _next_run(sched: dict) -> str | None:
    now = datetime.now(JST)
    kind = sched.get("kind", "")
    try:
        hh, mm = (int(x) for x in sched.get("time", "09:00").split(":")[:2])
    except Exception:
        hh, mm = 9, 0
    if kind == "weekly" and sched.get("dow"):
        dow_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
        targets = sorted(dow_map[d] for d in sched["dow"] if d in dow_map)
        for delta in range(0, 8):
            day = now + timedelta(days=delta)
            if day.weekday() in targets:
                cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand > now:
                    return cand.isoformat(timespec="minutes")
    if kind == "daily":
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand.isoformat(timespec="minutes")
    if kind == "runonce" and sched.get("at"):
        return sched["at"][:16]
    return None


def collect_crons() -> list[dict]:
    files = []
    files += list((WS / "cron.d").rglob("*.md"))
    files += list(WS.glob("goals/*/crons/**/*.md"))
    jobs = []
    seen_ids = set()
    for f in files:
        if "_archive" in f.parts:
            continue
        job = _parse_frontmatter(f)
        if not job or "id" not in job:
            continue
        jid = job["id"]
        if jid in SYSTEM_CRON_IDS or jid.startswith(SYSTEM_CRON_PREFIXES):
            continue
        if jid in seen_ids:
            continue
        seen_ids.add(jid)
        job["schedule_human"] = _human_schedule(job.get("schedule", {}))
        job["next_run_jst"] = _next_run(job.get("schedule", {}))
        jobs.append(job)
    order = {"cadb2b-muni-scan": 0, "cadb2b-content-weekly-digest": 1,
             "rbc-prepay-window-2027-01": 2, "zensurance-renewal-day-check": 3,
             "space-daily-refresh": 4, "weekly-niche-idea": 5}
    jobs.sort(key=lambda j: order.get(j["id"], 99))
    return jobs


# -------------------------------------------------------------- contract ---
def collect_contract() -> dict:
    root = WS / "contract-radar-seed"
    out: dict = {"available": root.exists()}
    if not root.exists():
        return out
    profile_dir = root / "profile"
    out["profile_files"] = sorted(p.name for p in profile_dir.glob("*.md")) if profile_dir.exists() else []
    leads_dir = root / "leads"
    out["leads_files"] = len([p for p in leads_dir.iterdir() if p.name != ".gitkeep"]) if leads_dir.exists() else 0
    out["scan_spec"] = (root / "scan" / "scan-spec.md").exists()
    return out


def main() -> None:
    snapshot: dict = {
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
    }
    try:
        snapshot["db"] = collect_db()
    except Exception as e:  # never fail the whole snapshot on one source
        snapshot["db"] = {"available": False, "error": str(e)}
    try:
        snapshot["content"] = collect_content()
    except Exception as e:
        snapshot["content"] = {"available": False, "error": str(e)}
    try:
        snapshot["crons"] = collect_crons()
    except Exception as e:
        snapshot["crons"] = []
        snapshot["crons_error"] = str(e)
    try:
        snapshot["contract"] = collect_contract()
    except Exception as e:
        snapshot["contract"] = {"available": False, "error": str(e)}
    if STATUS_PATH.exists():
        snapshot["status"] = json.loads(STATUS_PATH.read_text(encoding="utf-8"))

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {SNAPSHOT_PATH}")
    db = snapshot.get("db", {})
    if db.get("available"):
        print(f"  DB: {db['file_name']} {db['size_mb']} MB, tables={len(db.get('tables', {}))}")
        print(f"  federal={db.get('federal')}")
        m = db.get("municipal", {})
        print(f"  municipal: rows={m.get('snapshot_rows')} deduped={m.get('tenders_deduped')} "
              f"goods={m.get('goods_native_deduped')} leads={m.get('leads_count')} "
              f"total_cad={m.get('leads_total_cad')}")
    print(f"  content issues={snapshot.get('content', {}).get('issues_count')} "
          f"crons={len(snapshot.get('crons', []))}")


if __name__ == "__main__":
    main()
