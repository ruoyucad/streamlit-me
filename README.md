# 项目总看板（streamlit-me）

把 Qian 的几条项目线放进一个 Streamlit 看板，每天打开一屏看清：
**各线有多少数据、最近什么时候更新、下一步是什么。**

页面：总览（数据量 + 最近更新 + 各线状态卡）/ 联邦采购数据 / 市级扫描 /
内容引擎 / 接单线 / 定时任务。

## 数据是怎么来的（重要）

真实数据在本机，不在 git 里。所以：

- `tools/build_snapshot.py` 在本机收集最新状态（共享 DuckDB、内容引擎目录、
  cron 投影文件、`status.json`），写出 `data/snapshot.json`。
- `app.py` 默认只读 `data/snapshot.json` —— 本地和 Streamlit Community Cloud
  上跑的是同一套代码，看到的是同一份快照。
- 直连模式（可选，本机专用）：若设置环境变量 `CADB2B_DB_PATH` 指向 DuckDB
  文件，或仓库 `data/` 下放了 `cbintel.duckdb`，app 会优先直连读库。
  注意：当前 DuckDB 约 **445 MB**，超过 GitHub 100 MB 单文件上限，**不要**
  把它提交进仓库 —— 日常用快照模式即可。

手工维护的事实（Braintrust/Toptal 状态、房贷数字）都在根目录 **`status.json`**
里，改完重跑刷快照脚本即可，不要直接改 snapshot.json。

## 本地运行

```bash
pip install -r requirements.txt
python tools/build_snapshot.py   # 刷新 data/snapshot.json（需要能读到本机数据）
streamlit run app.py
```

## 刷新数据（日常）

```bash
python tools/build_snapshot.py
# 然后把 data/snapshot.json 推到 GitHub（Cloud 上约 1 分钟内生效）
```

## 部署到 Streamlit Community Cloud

1. 打开 https://share.streamlit.io ，用 GitHub 账号（ruoyucad）登录。
2. New app → 选择仓库 `ruoyucad/streamlit-me`、分支 `main`、主文件 `app.py`。
3. Deploy。之后每次往仓库推新的 `data/snapshot.json`，Cloud 会自动重启加载。

## 文件

| 文件 | 作用 |
|---|---|
| `app.py` | 看板本体（六个页面，侧边栏切换） |
| `tools/build_snapshot.py` | 本机刷快照脚本（唯一需要 duckdb 的地方） |
| `data/snapshot.json` | 已提交的快照数据，app 的默认数据源 |
| `status.json` | 手工维护：平台状态 + 房贷数字（改这里，别改快照） |
