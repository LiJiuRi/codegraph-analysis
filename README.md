# codegraph-analysis

把 `.codegraph` 代码索引变成**运营人员能读懂的业务分析手册**（Markdown 多文件）。

给 Claude/AI Agent 用的 [skill](https://code.claude.com/docs/skills)：读代码知识图谱，产出业务全景、模块详解、业务规则手册——让不懂代码的运营也能回答"这个系统管什么、一个流程怎么走、我的操作为什么被拦"。

## 产物形态

```
<目标项目>/docs/business-analysis/
├── README.md      总览：3 句大白话 + Mermaid 全景图 + 模块索引
├── 术语表.md       每词条一节、自包含（≥10 条）
├── 01-账单核心.md   每业务域一章，YAML frontmatter（域/模块/水位声明）
├── …结算/发票/财务对接/导出
├── 运营工作台.md    前端页面清单 + 页面↔业务域映射
└── 系统底座.md      纯技术模块一节带过
```

业务域文档骨架统一：职责定位 → 管哪些业务单据 → 核心业务流程 → 业务规则（四类）→ 上下游关系 → 功能清单 → 技术依据（折叠）。

## 业务规则四分类（每类带完整性水位）

| 类别 | 水位 | 挖掘抓手 |
|---|---|---|
| 校验规则 | 可穷举 | `throw new BusinessException("中文")` 模式扫描 |
| 状态与流转 | 词典穷举 / 流转推断 | 枚举 code+desc；setStatus 调用链 |
| 计算规则 | 代表性 | BigDecimal + `//中文公式` 行注释 |
| 边界规则 | 代表性 | 常量/配置/期限校验 |

每模块规则区头部**显式水位声明**（"校验规则穷举自 87 处异常拦截点；计算规则为代表性提取 8 条"），防止读者把代表性当全部。每条规则按**五要素**书写：条件、结论、例外、来源（file:line + 证据等级）、水位归属。

## 快速开始

```bash
# 前置：目标项目已建 codegraph 索引（本 skill 无索引不跑，不降级全仓库 grep）
codegraph init <项目根>        # 首次建索引（数分钟）
codegraph sync <项目根>        # 增量同步（秒级）

# 事实提取（只读 SQL，含索引健康度 preflight：缺库/缺表/空索引/过期 >20% → exit 2）
python -I scripts/graph_facts.py <项目根> -o facts.json

# 生成文档后强校验（12 项错误级 + 3 项警告级，exit 0 才算完成）
python -I scripts/verify_report.py <项目根>/docs/business-analysis
```

作为 skill 使用：把本仓库 clone 到 `~/.claude/skills/codegraph-analysis`，对 Claude 说"基于 .codegraph 索引给这个项目生成运营业务分析文档"。

## 仓库结构

```
├── SKILL.md               # skill 主工作流（AI 按此执行）
├── CONTEXT.md             # 领域术语表（业务域/四类规则/水位声明等）
├── scripts/
│   ├── graph_facts.py     # 只读 SQL → JSON 七块定量事实（模块/跨模块依赖/API/枚举/外部接口/热点/实体候选）
│   └── verify_report.py   # 输出文档结构契约校验器
├── assets/templates/      # 报告模板（自身通过校验器的参考实现）
└── evals/                 # 评测用例
```

## 设计红线

1. 无索引不跑——索引图谱是省 token 的根基（裸扫一个仓库 5~10 倍 token 且漏事实）
2. 正文零代码——`<details>` 折叠区外不得出现类名/SQL/注解
3. 推断必标注——状态词典是事实、流转路径是推断，混写即造假
4. 水位必声明——穷举与代表性显式区分
5. 校验不过不收官——`verify_report.py` 退出码非 0 不得宣称完成
