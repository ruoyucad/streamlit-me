"""项目总看板 — Qian 的各条项目线状态一屏看清。

数据模式：
  1) 直连模式：若环境变量 CADB2B_DB_PATH 指向一个 DuckDB 文件，或仓库内
     data/cbintel.duckdb 存在，则联邦/市级数据直接读库（本机使用）。
  2) 快照模式（默认）：只读 data/snapshot.json（由 tools/build_snapshot.py
     在本机生成后随仓库推送），本地与 Streamlit Cloud 行为一致。

快照缺段时显示「暂无数据」，不崩溃。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path(__file__).resolve().parent
SNAPSHOT_PATH = BASE / "data" / "snapshot.json"

st.set_page_config(page_title="项目总看板", layout="wide")


# ------------------------------------------------------------------ data ---
@st.cache_data(ttl=300)
def load_snapshot() -> dict:
    if SNAPSHOT_PATH.exists():
        try:
            return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _db_candidates() -> list[Path]:
    cands = []
    env = os.environ.get("CADB2B_DB_PATH")
    if env:
        cands.append(Path(env))
    cands.append(BASE / "data" / "cbintel.duckdb")
    return cands


@st.cache_data(ttl=300)
def load_live_db() -> dict | None:
    """直连模式：读本机 DuckDB 的联邦+市级关键数字；不可用则返回 None。"""
    db_path = next((p for p in _db_candidates() if p.exists()), None)
    if db_path is None:
        return None
    try:
        import duckdb
    except ImportError:
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        fed = {
            "contracts": con.execute("SELECT count(*) FROM fct_contract").fetchone()[0],
            "goods_contracts": con.execute(
                "SELECT count(*) FROM fct_contract WHERE is_goods").fetchone()[0],
            "suppliers": con.execute("SELECT count(*) FROM dim_supplier").fetchone()[0],
            "leaderboard_rows": con.execute(
                "SELECT count(*) FROM mart_supplier_leaderboard").fetchone()[0],
            "leaderboard_segments": con.execute(
                "SELECT count(DISTINCT segment) FROM mart_supplier_leaderboard").fetchone()[0],
            "open_goods_tenders": con.execute(
                "SELECT count(*) FROM mart_open_goods_tenders").fetchone()[0],
            "top_segments": [
                {"segment_zh": r[0], "segment_en": r[1], "n_suppliers": r[2], "value_m_cad": r[3]}
                for r in con.execute(
                    """SELECT segment_zh, segment_en, count(*),
                              round(sum(value_cad)/1e6, 1)
                       FROM mart_supplier_segment GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15""")
            ],
        }
        key = "coalesce(bid_number, detail_guid, '')"
        muni = {
            "snapshot_rows": con.execute("SELECT count(*) FROM stg_muni_tenders").fetchone()[0],
            "tenders_deduped": con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT source_portal, agency, {key} "
                "FROM stg_muni_tenders)").fetchone()[0],
            "goods_native_deduped": con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT source_portal, agency, {key} "
                "FROM stg_muni_tenders WHERE bid_classification='Goods')").fetchone()[0],
            "leads_count": con.execute("SELECT count(*) FROM mart_muni_goods_leads").fetchone()[0],
            "leads_total_cad": round(con.execute(
                "SELECT coalesce(sum(value_cad),0) FROM mart_muni_goods_leads").fetchone()[0]),
            "leads": [
                {"buyer": r[0], "title": (r[1] or "").strip(), "supplier": r[2],
                 "value_cad": r[3], "segment_zh": r[4],
                 "award_date": str(r[5]) if r[5] else None, "status": r[6]}
                for r in con.execute(
                    """SELECT buyer, title, supplier_raw, value_cad, segment_zh,
                              award_date, status FROM mart_muni_goods_leads
                       ORDER BY value_cad DESC""")
            ],
            "last_scraped_at_jst": str(
                con.execute("SELECT max(scraped_at) FROM stg_muni_tenders").fetchone()[0]),
        }
        con.close()
        return {"federal": fed, "municipal": muni,
                "as_of": datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
    except Exception:
        return None


SNAP = load_snapshot()
LIVE = load_live_db()
DB = SNAP.get("db", {})
FED = (LIVE or {}).get("federal") or DB.get("federal") or {}
MUNI = (LIVE or {}).get("municipal") or DB.get("municipal") or {}
CONTENT = SNAP.get("content", {})
CONTRACT = SNAP.get("contract", {})
STATUS = SNAP.get("status", {})
CRONS = SNAP.get("crons", [])
DATA_MODE = "DuckDB 直连" if LIVE else "快照"
DATA_AS_OF = (LIVE or {}).get("as_of") or DB.get("mtime_jst") or SNAP.get("generated_at_jst")


def fmt(n) -> str:
    if n is None:
        return "暂无数据"
    if isinstance(n, float):
        return f"{n:,.0f}"
    return f"{n:,}"


def fmt_cad(n) -> str:
    if n is None:
        return "暂无数据"
    return f"CAD {n:,.0f}"


def cron_by_id(cid: str) -> dict:
    return next((c for c in CRONS if c.get("id") == cid), {})


def short_dt(s) -> str:
    if not s:
        return "—"
    return str(s).replace("T", " ")[:16]


# ----------------------------------------------------------------- pages ---
def page_overview() -> None:
    st.title("总览")
    st.caption(f"数据来源：{DATA_MODE} ｜ 数据截至：{short_dt(DATA_AS_OF)} ｜ "
               f"快照生成：{short_dt(SNAP.get('generated_at_jst'))}")

    st.subheader("数据量（各线现有数据）")
    tables = DB.get("tables") or {}
    if tables:
        df = pd.DataFrame(
            [{"数据表": k, "行数": v} for k, v in sorted(tables.items(), key=lambda x: -(x[1] or 0))])
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("暂无数据（快照缺少数据表统计）")

    st.subheader("各线状态")
    muni_cron = cron_by_id("cadb2b-muni-scan")
    digest_cron = cron_by_id("cadb2b-content-weekly-digest")
    plat = STATUS.get("platforms", {})
    bt = plat.get("braintrust", {})
    mtg = STATUS.get("mortgage", {})

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("#### 🇨🇦 联邦采购数据（cadb2b）")
        st.metric("联邦合同", fmt(FED.get("contracts")))
        st.metric("供应商", fmt(FED.get("suppliers")))
        st.caption(f"在招货物标 {fmt(FED.get('open_goods_tenders'))} ｜ "
                   f"最近更新：{short_dt(DATA_AS_OF)}")
    with c2:
        st.markdown("#### 🏙️ 市级扫描（cadb2b-cities）")
        st.metric("市级标（去重）", fmt(MUNI.get("tenders_deduped")))
        st.metric("可跟进线索", fmt(MUNI.get("leads_count")),
                  fmt_cad(MUNI.get("leads_total_cad")))
        st.caption(f"最近扫描：{short_dt(MUNI.get('last_scraped_at_jst'))} ｜ "
                   f"下次：{short_dt(muni_cron.get('next_run_jst'))}"
                   f"（{muni_cron.get('schedule_human', '周一/周四')}）")
    with c3:
        st.markdown("#### 📰 内容引擎（cadb2b-content）")
        st.metric("已出期数", fmt(CONTENT.get("issues_count")))
        st.metric("订阅源", fmt(CONTENT.get("sources_count")))
        st.caption(f"最新：{CONTENT.get('latest_issue') or '—'} ｜ "
                   f"下次出稿：{short_dt(digest_cron.get('next_run_jst'))}"
                   f"（{digest_cron.get('schedule_human', '每周一')}）")

    c4, c5 = st.columns(2)
    with c4:
        st.markdown("#### 💼 接单线（数据工程合同）")
        st.metric("Braintrust", bt.get("status", "暂无数据"), bt.get("rate", ""))
        st.caption(f"可用时间：{bt.get('availability', '—')} ｜ "
                   f"状态日期：{bt.get('updated', '—')}")
    with c5:
        st.markdown("#### 🏠 房贷（提前还清）")
        st.metric("余额约", fmt_cad(mtg.get("balance_cad")),
                  f"原额 {fmt_cad(mtg.get('original_principal_cad'))}")
        st.caption(f"下一笔提前还款：{mtg.get('next_prepay_when', '—')} "
                   f"{fmt_cad(mtg.get('next_prepay_cad'))} ｜ 状态日期：{mtg.get('updated', '—')}")


def page_federal() -> None:
    st.title("联邦采购数据")
    if not FED:
        st.info("暂无数据")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("合同总数", fmt(FED.get("contracts")))
    c2.metric("其中货物合同", fmt(FED.get("goods_contracts")))
    c3.metric("供应商", fmt(FED.get("suppliers")))
    c4.metric("在招货物标", fmt(FED.get("open_goods_tenders")),
              f"适配中国货源 {fmt(FED.get('open_goods_tenders_china_fit'))}")
    st.caption(f"排行榜：{fmt(FED.get('leaderboard_rows'))} 行 / "
               f"{fmt(FED.get('leaderboard_segments'))} 个板块 ｜ "
               f"累计合同金额约 CAD {FED.get('total_value_b_cad', '—')}B（全量年份）")
    segs = FED.get("top_segments") or []
    if segs:
        st.subheader("供应商最多的板块（Top 板块）")
        df = pd.DataFrame(segs).rename(columns={
            "segment_zh": "板块", "segment_en": "Segment",
            "n_suppliers": "供应商数", "value_m_cad": "合同额（百万 CAD）"})
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("暂无板块数据")


def page_municipal() -> None:
    st.title("市级扫描")
    if not MUNI:
        st.info("暂无数据")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("抓取行数（快照）", fmt(MUNI.get("snapshot_rows")))
    c2.metric("去重标的", fmt(MUNI.get("tenders_deduped")))
    c3.metric("原生 Goods 标", fmt(MUNI.get("goods_native_deduped")))
    c4.metric("线索总额", fmt_cad(MUNI.get("leads_total_cad")),
              f"{fmt(MUNI.get('leads_count'))} 条")
    st.caption(f"最近扫描：{short_dt(MUNI.get('last_scraped_at_jst'))} ｜ "
               "扫描节奏：每周一/周四（定时任务 cadb2b-muni-scan）")
    leads = MUNI.get("leads") or []
    if leads:
        st.subheader("可跟进线索（按金额排序，需人工复核）")
        df = pd.DataFrame(leads).rename(columns={
            "buyer": "买家", "title": "标的", "supplier": "中标供应商",
            "value_cad": "金额 CAD", "segment_zh": "品类",
            "award_date": "中标日期", "status": "状态"})
        cols = [c for c in ["买家", "标的", "中标供应商", "金额 CAD", "品类", "中标日期", "状态"]
                if c in df.columns]
        st.dataframe(df[cols], use_container_width=True, hide_index=True)
    else:
        st.info("暂无线索数据")


def page_content() -> None:
    st.title("内容引擎")
    if not CONTENT.get("available"):
        st.info("暂无数据")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("已出期数", fmt(CONTENT.get("issues_count")))
    c2.metric("订阅源", fmt(CONTENT.get("sources_count")))
    c3.metric("已看条目（seen）", fmt(CONTENT.get("seen_entries")))
    cron = cron_by_id("cadb2b-content-weekly-digest")
    st.caption(f"出稿节奏：{cron.get('schedule_human', '每周一 ~08:23')} ｜ "
               f"下次：{short_dt(cron.get('next_run_jst'))} ｜ 只出稿给 Qian 审，不自动发布")
    issues = CONTENT.get("issues") or []
    if issues:
        st.subheader("期数列表")
        df = pd.DataFrame(issues).rename(columns={
            "name": "期号/文件", "is_dir": "文件夹", "mtime_jst": "更新时间"})
        st.dataframe(df, use_container_width=True, hide_index=True)


def page_contract() -> None:
    st.title("接单线")
    plat = STATUS.get("platforms", {})
    bt = plat.get("braintrust", {})
    tt = plat.get("toptal", {})
    st.subheader(f"Braintrust — {bt.get('status', '暂无数据')}")
    st.caption(f"费率：{bt.get('rate', '—')} ｜ 可用时间：{bt.get('availability', '—')} ｜ "
               f"状态日期：{bt.get('updated', '—')}")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**已完成**")
        for item in bt.get("done", []):
            st.markdown(f"- ✅ {item}")
        if not bt.get("done"):
            st.markdown("- 暂无数据")
    with col2:
        st.markdown("**待办**")
        for item in bt.get("pending", []):
            st.markdown(f"- ⬜ {item}")
        if not bt.get("pending"):
            st.markdown("- 暂无数据")
    if CONTRACT.get("available"):
        st.caption(f"资料包文件：{', '.join(CONTRACT.get('profile_files', [])) or '—'} ｜ "
                   f"岗位扫描规范：{'有' if CONTRACT.get('scan_spec') else '无'} ｜ "
                   f"线索文件：{CONTRACT.get('leads_files', 0)}")
    st.divider()
    st.subheader(f"Toptal — {tt.get('status', '暂无数据')}")
    st.write(tt.get("note", "暂无数据"))


def page_crons() -> None:
    st.title("定时任务")
    if not CRONS:
        st.info("暂无数据")
        return
    line_map = {
        "cadb2b-muni-scan": "市级扫描", "cadb2b-content-weekly-digest": "内容引擎",
        "rbc-prepay-window-2027-01": "房贷", "zensurance-renewal-day-check": "保险续保",
        "space-daily-refresh": "项目看板", "weekly-niche-idea": "每周点子",
    }
    rows = [{
        "任务": c.get("title") or c.get("id"),
        "节奏": c.get("schedule_human", "—"),
        "下次运行": short_dt(c.get("next_run_jst")),
        "所属线": line_map.get(c.get("id"), c.get("owner", "—")),
        "启用": "✅" if c.get("enabled") else "⏸️",
    } for c in CRONS]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("注：上次运行结果在各任务的运行记录里；本表由 cron 投影文件解析生成。")


PAGES = {
    "总览": page_overview,
    "联邦采购数据": page_federal,
    "市级扫描": page_municipal,
    "内容引擎": page_content,
    "接单线": page_contract,
    "定时任务": page_crons,
}

choice = st.sidebar.radio("项目线", list(PAGES.keys()))
st.sidebar.caption(f"数据模式：{DATA_MODE}\n\n数据截至：{short_dt(DATA_AS_OF)}")
PAGES[choice]()
