# 智能取数 Agent

用中文提问，自动生成 SQL 并执行，把结果以表格返回。面向客户营销类分析场景的 Text-to-SQL 智能体。

## 它解决什么问题

业务同学想查一个数，通常要走「提需求 → 排队 → 开发写 SQL → 返数」的流程，慢且反复。这个项目把这一步压缩成一句话：输入「上季度末资产过百万的客户有多少」，系统自己判断该查哪张表、哪些字段、怎么过滤，生成 PostgreSQL 语句、做安全校验、执行，最后给出结果和实际执行的 SQL。

实现上没有把整库表结构一次性塞给模型，而是做了三层约束：**元数据层**用结构化的表/字段/枚举/业务口径描述替代建表语句；**编排层**让模型通过 Function Calling 按需取表结构、查指标定义，最多 12 轮；**校验层**在 SQL 执行前用 sqlglot 做静态安全检查，只放行只读查询。

效果上，内部题库 150 道（143 道自建 + 7 道公开样例）目前全部通过，但这反映的是对内部题库的拟合程度，**不作为泛化能力对外表述**。真正用于观察泛化的是冻结留出集：16 题中 15 题计分，单次评测通过 12 题（80%）。失败集中在口径细节（月份格式、代码值与中文名、空集返回 0 还是 NULL），而不是查不到表。

## 适用场景

- 客户、资产、持仓、交易、资金流水等结构化指标的自助查询
- 需要把业务口径（如"平均总资产要不要算无资产客户"）显式沉淀下来的团队
- 作为 Text-to-SQL 的工程骨架：元数据组织、Agent 编排、SQL 安全围栏、可复现评测

不适合：非结构化文本检索、跨库联邦查询、需要强事务一致性的写入场景。

## 系统架构

```
用户提问
   │
   ├─ 接入层      app.py（Gradio）/ main.py（命令行、代码调用）
   │
   ├─ 规则注入    metadata.global_rules + 正则路由，只注入命中的业务口径
   │
   ├─ 元数据层    metadata.json（8 表 / 68 字段 / 26 条口径 / 4 个指标）
   │              metadata_tools.py：search_table / get_table_schema / get_metric
   │              schema_retriever.py：BM25 表召回 + join 图补全（可选，预取模式）
   │
   ├─ 编排层      run_agent：多轮 Function Calling，同工具重复调用即强制出 SQL
   │
   ├─ 校验层      check_question_sql_consistency（问句-SQL 一致性）
   │              security_guard.py（sqlglot 静态围栏）
   │
   ├─ 执行层      execute_sql：只读会话 + 语句超时 + 结果行数上限
   │
   └─ 输出        SQL + 结果表格 + 失败阶段归因
```

关键设计取舍：

- **元数据而不是 DDL**：表结构、枚举取值、字段中文名、关联键、业务口径集中在 `metadata/metadata.json`，同一份文件同时供提示词、检索、安全白名单使用。代价是一处描述写错会同时影响三处。
- **工具调用而不是全量上下文**：模型按需查表，上下文只包含候选表；`SCHEMA_RETRIEVAL_MODE=prefetch` 时可先做一次检索，把候选表结构直接拼入提示词。
- **静态围栏而不是只靠数据库权限**：执行前先用 sqlglot 解析，拦住非 SELECT、多语句、写操作、危险函数、非白名单表、无 LIMIT 的明细查询。

## 快速开始

### 环境依赖

- Python 3.10 及以上（开发与测试环境为 3.10.11）
- PostgreSQL，需已建库并导入以下 8 张表

| 表 | 说明 |
|---|---|
| ads_cust_info_d | 客户信息快照 |
| dws_cust_aset_d | 客户资产日汇总 |
| dws_cust_fin_d | 客户资金流水日事实 |
| dwd_cust_hold_d | 客户持仓日事实 |
| dwd_cust_tran_d | 客户交易买卖日事实 |
| dim_product | 产品属性维表 |
| dim_branch | 营业部维表 |
| dim_public | 标准化编码字典 |

```bash
pip install -r requirements.txt
```

`requirements-lock.txt` 是同一组直接依赖的精确版本，需要贴近已验证环境时使用。

> **仓库自带一份程序化合成的演示数据**，可让陌生读者在十分钟内把链路跑通。数据由固定随机种子生成，**不含任何真实客户、真实机构或原始数据集的记录**，仅用于演示，与生产数据无关。建表脚本在 `scripts/schema.sql`，造数脚本在 `scripts/seed_demo_data.py`。

### 第一步：准备演示库

需要一个可创建数据库的 PostgreSQL 账号。以下命令只新建一个独立演示库，不会改动已有数据库。

```bash
# 任选其一：手工建库
psql -U postgres -c "CREATE DATABASE askdata_demo ENCODING 'UTF8'"

# 或交给脚本自动创建
python scripts/seed_demo_data.py --create-db --recreate
```

### 第二步：建表 + 导入合成数据

```bash
# 方式 A：一条命令建表并导入（推荐，会先 DROP 同名表）
python scripts/seed_demo_data.py --create-db --recreate

# 方式 B：先用 psql 建表，再导入
psql -U postgres -d askdata_demo -f scripts/schema.sql
python scripts/seed_demo_data.py
```

导入规模：客户 500、产品 240、营业部 10（4 个分公司）、持仓 1431、交易 1284、资金流水 2218、资产 469。约 8% 客户刻意不生成资产与持仓记录，用于演示"无资产客户"这类口径。

脚本结束时会跑 7 项自检（持仓合计与资产是否对得上、维度是否孤立、城市与营业部是否矛盾、年龄与出生年份是否一致、交易日期是否越界等）。只做校验不写库：

```bash
python scripts/seed_demo_data.py --check
```

数据口径：

- 客户快照 `20260531`，资产与持仓快照 `20260331`，交易覆盖 2026 年一季度工作日，资金流水从 `20251231` 起共 4 个月末。
- 普通账户总资产 = 普通账户现金 + 普通账户持仓市值合计，**按构造精确相等**（造数时先分配持仓再倒推现金），不是近似值。
- 省份、城市、营业部三者绑定生成，客户不会出现"省份与营业部所在地矛盾"。
- 同一随机种子（`20260531`）重跑结果完全一致，可用 `--hash-only` 对比内容哈希。

### 第三步：配置

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

`DEEPSEEK_API_KEY` 与 `DB_PASSWORD` 必须自己填，其余项有默认值。若使用演示库，把 `.env` 里的 `DB_NAME` 改成 `askdata_demo`（或提前 `set DB_NAME=askdata_demo`）。数据库地址、库名、用户名等默认值见 `.env.example`。`.env` 已在 `.gitignore` 中，不会被提交。

### 第四步：启动

```bash
# Web 界面（Gradio，默认监听 127.0.0.1:7860）
python app.py

# 命令行单次查询（问题写在 main.py 末尾的示例列表里）
python main.py

# 代码内调用
python -c "from main import run_agent; r = run_agent('男性客户有多少个', return_result=True); print(r.columns, r.rows)"
```

不接大模型也可以先验证数据库链路：

```bash
python -c "import main; print(main.execute_sql(\"SELECT COUNT(*) FROM ads_cust_info_d WHERE data_dt='20260531'\"))"
```

## 评测

### 套件构成

评测集定义在 `evaluation/suites.json`，共 7 个入口：

| 套件 | 题量 | 说明 |
|---|---|---|
| `compact90` | 90 | 默认；三个难度各 30 题，按题号固定等距抽取，不依据模型历史得分挑题 |
| `official7` | 7 | 来自公开 Q&A 样例，只说明兼容性 |
| `generalization30` | 30 | 同一批基准 SQL 的自然语言改写，用于观察表达泛化 |
| `heldout16` | 16 | 第一版留出集（已用于迭代，不再作为无偏指标） |
| `heldout_v2` / `heldout_v2_1` | 各 16 | 冻结留出集；v2.1 仅修正了其中 1 题的基准口径 |
| `full150` | 150 | 内部完整题库 |

### 判定口径

整题通过需要**同时**满足字段语义与结果值：

- **结果值**：行数必须一致；数值按 0.01 容差比较；排名类题目才校验排序，并列值内允许互换；空结果对空结果判通过。
- **字段语义**：先比列数，再逐列比对语义。语义由 `SEMANTIC_ALIASES`、SQL 表达式推断、`field_contracts.json` 三级决定；都不命中时退化为列名字面比较，因此"数据正确但列名不同"会被判失败。
- **不计分题**：题目标注 `expected_behavior=clarify` 时不执行、不计入准确率分母。因此同一份结果会出现两个分母，引用时必须写明是 12/15（计分）还是 12/16（含不计分）。

### 复现

```bash
# 首次评测：仓库未附带快照清单，需先生成
python evaluation/snapshot_manifest.py --write

# 校验数据与清单一致（不一致会直接停止，防止在变动数据上比较成绩）
python evaluation/snapshot_manifest.py --verify

# 跑评测
python evaluation/evaluate.py                      # 默认 compact90
python evaluation/evaluate.py --suite heldout_v2

# 同一配置连跑 3 次后汇总波动
python evaluation/summarize_runs.py \
  evaluation/eval_results_v2_compact90_<t1>.json \
  evaluation/eval_results_v2_compact90_<t2>.json \
  evaluation/eval_results_v2_compact90_<t3>.json \
  --output evaluation/compact90_stability.json
```

每份结果 JSON 会记录模型参数、提示词哈希、题库与元数据哈希、代码快照哈希、数据快照清单哈希、依赖版本，用于判断两次成绩是否真的可比。

### 结果口径

- 内部题库（150 题，含 7 道公开样例）当前全部通过。这是**自建题集上的拟合结果**，不是泛化能力。
- 冻结留出集 v2：15 道计分题通过 12 道（80%），单次评测，冻结后未再改动题目。
- 以上数字均在原始数据集上测得。**仓库自带的合成数据只用于跑通链路，规模与分布都不同，不能复现这些分数**；在合成库上跑评测得到的数字没有比较意义。
- 未在公开基准（如 Spider、BIRD）上评测，**不宣称任何公开基准成绩**。
- 留出集题量小，单题即影响 6.7 个百分点；同一代码在 temperature=0 下重跑仍会出现 1~3 题的波动，因此更适合看逐题失败原因，而不是只看总准确率。

## 已知限制

1. **多表深层关联不稳定**：四表以上的查询依赖检索是否把中间表全部召回；召回缺失时模型会在错误的表上编造字段。
2. **口径细节无统一约定**：月份返回 `202601` 还是 `01`、等级返回代码还是中文名、空集返回 `NULL` 还是 `0`，模型输出不稳定，评测可能因此判为失败（数据本身正确）。
3. **掩码数据下按名称聚合会出错**：营业部名称脱敏后存在同名不同 ID，按名称分组会把不同实体合并成一个。此类题应按 ID 统计。
4. **结果超过 5000 行直接报错**：没有分页或截断降级，模型只能改写查询缩小范围。
5. **多轮指代未经充分验证**：Web 界面保留最近 10 轮展示历史，但 Agent 主流程不消费历史，评测也只跑单轮，"那上个月呢"这类追问尚未验证。
6. **歧义问题没有反问机制**：标为 `clarify` 的题目不会触发澄清提问，评测也不执行这类题，真实行为未验证。
7. **元数据驱动带来维护成本**：新增业务域需要同步扩 `metadata.json`、检索词表与业务规则，否则会漏召回。
8. **安全围栏是黑名单+白名单组合，不是完备防护**：未限定表别名的列名不校验、函数黑名单为枚举制，最终依赖只读数据库账号兜底。

## 目录结构

```
ask-data-agent/
├── app.py                  # Gradio Web 界面
├── main.py                 # Agent 主流程、提示词、执行与重试
├── metadata_tools.py       # 元数据工具（查表/查字段/查指标/规则匹配）
├── schema_retriever.py     # BM25 表召回 + join 图补全 + 取值采样
├── security_guard.py       # SQL 静态安全围栏
├── config.py               # 配置加载（.env + 默认值）
├── metadata/
│   └── metadata.json       # 表结构、字段、枚举、关联、业务口径、指标
├── scripts/
│   ├── schema.sql          # 8 张演示表的建表脚本
│   └── seed_demo_data.py   # 合成数据生成与导入（固定种子，可重复）
├── evaluation/
│   ├── evaluate.py         # 评测主程序（判定 + 指纹落盘 + 离线重放）
│   ├── suites.json         # 套件注册
│   ├── field_contracts.json# 单题字段语义与排序合同
│   ├── test_questions.json # 150 题题库与基准 SQL
│   ├── generalization_questions.json  # 30 道改写题
│   ├── heldout_spec.json / heldout_v2_spec.json / heldout_v2_1_spec.json
│   ├── heldout_*_manifest.json        # 冻结凭据（spec 哈希）
│   ├── build_questions.py             # 题库构建
│   ├── build_generalization_questions.py
│   ├── heldout_builder.py             # 留出集查重 / SQL 校验 / 冻结
│   ├── snapshot_manifest.py           # 数据库快照清单（--write / --verify）
│   └── summarize_runs.py              # 多次运行汇总
├── tests/                  # 109 个单元与回归测试（unittest，可离线运行）
├── requirements.txt
├── requirements-lock.txt
└── .env.example
```

## 安全边界

- 只允许单条 `SELECT` / `WITH`，写操作、DDL、事务语句、数据修改型 CTE 一律拒绝。
- 表名与字段名按 `metadata.json` 白名单校验；危险函数（读文件、睡眠、改配置等）拦截。
- 无 `LIMIT`、无 `GROUP BY`、无聚合的明细查询会被拒绝执行。
- 每次查询使用独立只读连接，默认语句超时 10 秒、最多返回 5000 行，可通过 `.env` 调整。
- API Key 与数据库口令只从环境变量读取，不写入任何评测产物。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不依赖真实数据库与 API Key：数据库连接、模型调用、快照校验均被替换或显式关闭，可在本地直接运行。
