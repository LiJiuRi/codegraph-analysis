#!/usr/bin/env python3
"""verify_report.py — 校验 codegraph-analysis 生成的报告目录是否符合结构契约。

退出码: 0 = 通过（可含警告） / 1 = 存在错误级违规
用法: python -I verify_report.py <报告目录>
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

TYPES = {"总览", "术语表", "业务域", "工作台", "底座"}
DOMAIN_KEYS = ("业务域", "模块", "水位声明")
SKELETON = ("职责", "单据", "流程", "业务规则", "上下游", "功能")
RULE_SECTIONS = ("校验规则", "状态与流转", "计算规则", "边界规则")
GENERIC_FLOW_TITLES = {"核心流程", "主要业务流程"}
PLACEHOLDERS = ("TODO", "TBD", "待补充", "我无法", "占位")
CREDENTIAL = re.compile(r"(?i)(sk-[a-z0-9_-]{12,}|authorization:|api[_ -]?key\s*[:=])")
QUALIFIED_NAME = re.compile(r"\b(?:com|cn|org|net)\.[a-z][\w.]*")
SQLISH = re.compile(r"(?i)\bselect\b[\s\S]{0,80}?\bfrom\b")
JAVA_FILE = re.compile(r"[\w/$-]+\.java\b")
ANNOTATION = re.compile(r"@[A-Z]\w+")
MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)#\s]+\.md)(#[^)\s]*)?\)")
ORDERED_ITEM = re.compile(r"^\s*\d+[\.、] ", re.M)
FENCED = re.compile(r"```[\s\S]*?```")
DETAILS = re.compile(r"<details>[\s\S]*?</details>", re.I)

errors: list[str] = []
warnings: list[str] = []


def err(code: str, msg: str) -> None:
    errors.append(f"[{code}] {msg}")


def warn(code: str, msg: str) -> None:
    warnings.append(f"[{code}] {msg}")


def strip_details(text: str) -> str:
    return DETAILS.sub("", text)


def visible_body(text: str) -> str:
    """正文 = 折叠区外 + 非 fenced 代码块（图源码与行内证据不算泄漏）。"""
    body = strip_details(text)
    return FENCED.sub("", body)


def frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def headings(text: str, level: int) -> list[str]:
    return re.findall(rf"^{'#' * level}\s+(.+)\s*$", text, re.M)


def check_domain_doc(path: Path, text: str, fm: dict) -> None:
    rel = path.name
    for key in DOMAIN_KEYS:
        if not fm.get(key):
            err("E2", f"{rel}: frontmatter 缺必填键「{key}」")
    joined = "\n".join(headings(text, 2)) + "\n".join(headings(text, 3))
    for kw in SKELETON:
        if not any(kw in h for h in headings(text, 2)):
            err("E3", f"{rel}: 缺骨架章节（关键词「{kw}」）")
    for sec in RULE_SECTIONS:
        if sec not in joined:
            err("E4", f"{rel}: 业务规则区缺小节「{sec}」")
    rules = text.split("## 业务规则", 1)
    scope = rules[1].split("\n## ", 1)[0] if len(rules) == 2 else text
    if not re.search(r"穷举|代表性", scope):
        err("E5", f"{rel}: 业务规则区缺水位声明（穷举/代表性 + 来源数量）")
    for h in headings(text, 3):
        if h.strip() in GENERIC_FLOW_TITLES:
            err("E7", f"{rel}: 流程标题「{h}」是泛化词，须用运营问法（如「账单创建流程是怎么走的」）")
    flow = text.split("## 核心业务流程", 1)
    if len(flow) == 2 and "推断" not in flow[1].split("\n## ", 1)[0]:
        err("E9", f"{rel}: 流程区缺「推断」标注（流转路径源自调用链推断，必须显式标注）")
    # E6: 每个 mermaid 块后须有等价文字步骤
    for m in re.finditer(r"```mermaid[\s\S]*?```", text):
        after = text[m.end():]
        nxt = re.search(r"^## ", after, re.M)
        segment = after[:nxt.start()] if nxt else after
        if not ORDERED_ITEM.search(segment):
            err("E6", f"{rel}: mermaid 图后缺等价文字步骤（有序列表）")
            break


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python -I verify_report.py <报告目录>", file=sys.stderr)
        return 1
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"不是目录: {root}", file=sys.stderr)
        return 1
    docs = sorted(root.glob("*.md"))
    names = {d.name for d in docs}

    # E1 必需文件
    for req in ("README.md", "术语表.md", "运营工作台.md", "系统底座.md"):
        if req not in names:
            err("E1", f"缺必需文件 {req}")
    if not any(re.match(r"\d+-", n) for n in names):
        err("E1", "缺业务域章节（至少一个「数字-」前缀的 .md）")
    if errors:
        return finish()

    for d in docs:
        text = d.read_text(encoding="utf-8")
        fm = frontmatter(text)
        rel = d.name
        if d.stat().st_size > 100 * 1024:
            warn("W3", f"{rel}: 超过 100KB，考虑拆分")
        for ph in PLACEHOLDERS + ("{{", "}}"):
            if ph in text:
                err("E11", f"{rel}: 占位符泄漏「{ph}」")
        if CREDENTIAL.search(text):
            err("E12", f"{rel}: 疑似凭据内容")
        if not fm.get("type"):
            err("E2", f"{rel}: 缺 frontmatter 或缺 type 键")
            continue
        if fm["type"] not in TYPES:
            err("E2", f"{rel}: type 非法「{fm['type']}」，合法值 {sorted(TYPES)}")
            continue
        body = visible_body(text)
        for pat, tag in ((JAVA_FILE, ".java 文件名"), (SQLISH, "SQL 语句"),
                         (ANNOTATION, "@注解"), (QUALIFIED_NAME, "全限定类名")):
            hit = pat.search(body)
            if hit:
                err("E8", f"{rel}: 正文出现代码痕迹 {tag}「{hit.group(0)[:40]}」（须移入 <details> 证据区）")
        if fm["type"] == "业务域":
            check_domain_doc(d, text, fm)
        if rel == "README.md":
            if "```mermaid" not in text:
                err("E10", "README.md: 缺全景图（mermaid）")
            for m in MD_LINK.finditer(text):
                target = (d.parent / m.group(2)).resolve()
                if not target.is_file():
                    err("E10", f"README.md: 索引链接失效 {m.group(2)}")
        if fm["type"] == "术语表":
            terms = len(re.findall(r"^\*\*[^*]+\*\*:", text, re.M))
            if terms < 10:
                warn("W1", f"术语表仅 {terms} 词条（<10）")
        if fm["type"] == "工作台":
            rows = len(re.findall(r"^\|.*\|$", text, re.M))
            if rows < 7:  # 表头+分隔+5 行
                warn("W2", f"运营工作台表格行数不足（{max(rows - 2, 0)} 行 <5）")
    return finish()


def finish() -> int:
    for w in warnings:
        print(w, file=sys.stderr)
    for e in errors:
        print(e, file=sys.stderr)
    print(f"verify_report: {len(errors)} 错误, {len(warnings)} 警告", file=sys.stderr)
    if errors:
        print("未通过 — 修复上述错误后重跑，不得宣称完成。", file=sys.stderr)
        return 1
    print("通过 — 结构契约满足。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
