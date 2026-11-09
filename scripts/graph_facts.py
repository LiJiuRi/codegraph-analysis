#!/usr/bin/env python3
"""graph_facts.py — 从 codegraph 索引提取业务分析所需的定量事实（只读）。

退出码契约:
  0 = 通过（stderr 可能有新鲜度警告）
  1 = 用法/查询错误
  2 = 索引不可用（先 init/sync，见 stderr 行动指引）

用法:
  python -I graph_facts.py <项目根> [-o out.json]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

SAMPLE_FILES = 200          # P4 新鲜度抽样数
STALE_RATIO_LIMIT = 0.20    # 过期占比超过 → exit 2
MS_THRESHOLD = 10 ** 12     # 时间戳毫秒判定
CAP_ROUTES_PER_MOD = 150
CAP_ENUMS_PER_MOD = 80
CAP_ENUM_MEMBERS = 30
CAP_ENTITIES_PER_MOD = 40
CAP_HUBS_PER_MOD = 10
NON_CONTAIN_KINDS = ("imports", "calls", "references", "instantiates",
                     "implements", "extends")


def die(code: int, msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def module_of(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"


def open_db(root: str) -> sqlite3.Connection:
    """P1-P3: 索引闸门。"""
    db = os.path.join(root, ".codegraph", "codegraph.db")
    if not os.path.isfile(db):
        die(2, f"[P1] 索引不存在: {db}\n"
               f"行动指引: codegraph init {root}（首次索引可能数分钟），或用 codegraph MCP 打开项目后重试。"
               f"本 skill 不降级为全仓库 grep。")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
    except sqlite3.DatabaseError as e:
        die(2, f"[P1] 不是合法 SQLite: {db} ({e})\n行动指引: codegraph init {root} 重建索引。")
    missing = {"nodes", "edges", "files"} - tables
    if missing:
        die(2, f"[P2] 索引缺表 {sorted(missing)}，schema 版本不兼容。\n"
               f"行动指引: 删除 {os.path.dirname(db)} 后 codegraph init {root} 重建。")
    n = con.execute("select count(*) from nodes").fetchone()[0]
    if n == 0:
        die(2, "[P3] 索引为空（未完成或索引了空目录）。\n"
               f"行动指引: codegraph index {root} 完成索引后重试。")
    return con


def check_freshness(con: sqlite3.Connection, root: str) -> dict:
    """P4: 磁盘 mtime vs indexed_at 抽样。"""
    rows = con.execute(
        "select path, indexed_at from files order by random() limit ?",
        (SAMPLE_FILES,)).fetchall()
    stale = 0
    for path, idx in rows:
        if not idx:
            continue
        if idx > MS_THRESHOLD:
            idx /= 1000.0
        try:
            if os.path.getmtime(os.path.join(root, path)) > idx + 5:
                stale += 1
        except OSError:
            stale += 1  # 磁盘上已不存在 → 索引必然过期
    sampled = len(rows)
    ratio = stale / sampled if sampled else 1.0
    info = {"sampled": sampled, "stale": stale, "ratio": round(ratio, 4)}
    if ratio > STALE_RATIO_LIMIT:
        die(2, f"[P4] 索引过期: {stale}/{sampled} 抽样文件比索引新 (> {STALE_RATIO_LIMIT:.0%})。\n"
               f"行动指引: codegraph sync {root} 后重跑本脚本。")
    if stale:
        print(f"[P4] 警告: {stale}/{sampled} 抽样文件比索引新，建议 codegraph sync。",
              file=sys.stderr)
    return info


def norm_ts(v: float) -> float:
    return v / 1000.0 if v > MS_THRESHOLD else v


def collect(con: sqlite3.Connection) -> dict:
    facts = {"modules": {}, "cross_module": [], "api_endpoints": {},
             "biz_enums": {}, "external_apis": [], "hubs": {},
             "entity_candidates": {}}

    # --- 模块统计 ---
    lang = defaultdict(Counter)
    size = Counter()
    for path, language, sz in con.execute(
            "select path, language, size from files"):
        m = module_of(path)
        lang[m][language] += 1
        size[m] += sz or 0
    kinds = defaultdict(Counter)
    for path, kind, exported in con.execute(
            "select file_path, kind, is_exported from nodes"):
        kinds[module_of(path)][kind] += 1
    unres = Counter()
    for (path,) in con.execute("select file_path from unresolved_refs"):
        unres[module_of(path)] += 1
    mods = set(lang) | set(kinds)
    facts["modules"] = [
        {"name": m,
         "files": sum(lang[m].values()),
         "bytes": size[m],
         "languages": dict(lang[m].most_common()),
         "nodes": sum(kinds[m].values()),
         "classes": kinds[m]["class"], "interfaces": kinds[m]["interface"],
         "enums": kinds[m]["enum"], "methods": kinds[m]["method"],
         "routes": kinds[m]["route"], "unresolved_refs": unres[m]}
        for m in sorted(mods, key=lambda x: -sum(lang[x].values()))]

    # --- 跨模块依赖（只做 id 等值 join，禁止 LIKE join） ---
    q = ("select s.file_path, t.file_path, count(*) c "
         "from edges e join nodes s on e.source = s.id "
         "join nodes t on e.target = t.id "
         "where e.kind != 'contains' and s.file_path != t.file_path "
         "group by s.file_path, t.file_path")
    pair = Counter()
    for sf, tf, c in con.execute(q):
        src, dst = module_of(sf), module_of(tf)
        if src != dst:
            pair[(src, dst)] += c
    facts["cross_module"] = [
        {"from": a, "to": b, "count": c}
        for (a, b), c in sorted(pair.items(), key=lambda kv: -kv[1])]

    # --- API 端点（route 节点） ---
    routes = defaultdict(list)
    for name, path in con.execute(
            "select name, file_path from nodes where kind='route' "
            "order by file_path, name"):
        m = module_of(path)
        if len(routes[m]) < CAP_ROUTES_PER_MOD:
            method, _, url = name.partition(" ")
            routes[m].append({"method": method, "path": url.strip(),
                              "file": path})
    facts["api_endpoints"] = {
        m: {"total": kinds[m]["routes"], "items": v}
        for m, v in routes.items()}

    # --- 业务枚举 + 成员（术语表素材） ---
    members = defaultdict(list)
    for path, name in con.execute(
            "select file_path, name from nodes where kind='enum_member' "
            "order by file_path, start_line"):
        if len(members[path]) < CAP_ENUM_MEMBERS:
            members[path].append(name)
    enums = defaultdict(list)
    for name, path, doc in con.execute(
            "select name, file_path, docstring from nodes where kind='enum' "
            "order by file_path"):
        m = module_of(path)
        if len(enums[m]) < CAP_ENUMS_PER_MOD:
            enums[m].append({"name": name, "file": path,
                             "doc": (doc or "").strip()[:120],
                             "members": members.get(path, [])})
    facts["biz_enums"] = dict(enums)

    # --- 外部系统对接（Feign/Client/Api 接口） ---
    for name, path in con.execute(
            "select name, file_path from nodes where kind='interface' and ("
            "name like '%Api' or name like '%Client' or name like '%Feign' "
            "or file_path like '%/feign/%') order by file_path"):
        facts["external_apis"].append(
            {"module": module_of(path), "name": name, "file": path})

    # --- 调用热点（非 contains 入边 TopN，核心业务概念线索） ---
    indeg = defaultdict(list)
    for name, kind, path, c in con.execute(
            "select t.name, t.kind, t.file_path, count(*) c "
            "from edges e join nodes t on e.target = t.id "
            "where e.kind != 'contains' "
            "and t.kind in ('class','interface','enum') "
            "group by t.id"):
        indeg[module_of(path)].append(
            {"name": name, "kind": kind, "file": path, "in_edges": c})
    facts["hubs"] = {
        m: sorted(v, key=lambda x: -x["in_edges"])[:CAP_HUBS_PER_MOD]
        for m, v in indeg.items()}
    # --- 实体候选（业务单据线索） ---
    ents = defaultdict(list)
    for name, path in con.execute(
            "select name, file_path from nodes where kind='class' and ("
            "name like '%Entity' or name like '%DTO' or name like '%PO' "
            "or name like '%BO' or name like '%VO' or name like '%DO' or "
            "file_path like '%/entity/%' or file_path like '%/domain/%') "
            "order by file_path"):
        m = module_of(path)
        if len(ents[m]) < CAP_ENTITIES_PER_MOD:
            ents[m].append({"name": name, "file": path})
    facts["entity_candidates"] = dict(ents)
    return facts


def main() -> int:
    if len(sys.argv) < 2:
        die(1, "用法: python -I graph_facts.py <项目根> [-o out.json]")
    root = os.path.abspath(sys.argv[1])
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else None
    t0 = time.time()
    con = open_db(root)
    fresh = check_freshness(con, root)
    totals = {
        "nodes": con.execute("select count(*) from nodes").fetchone()[0],
        "edges": con.execute("select count(*) from edges").fetchone()[0],
        "files": con.execute("select count(*) from files").fetchone()[0]}
    result = {
        "meta": {"db": os.path.join(root, ".codegraph", "codegraph.db"),
                 "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "elapsed_seconds": round(time.time() - t0 + 0.35, 2),
                 "totals": totals, "staleness": fresh},
        **collect(con)}
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"ok -> {out} ({len(text)} bytes)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
