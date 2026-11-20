# 住户出行调查配权 API

基于 **FastAPI + SQLite + 原生 HiGHS 线性规划引擎** 的应答户权重校准服务，
支持 `docker compose` 一键启动。

## 业务规则

- 两份源表均可**整份换交**（历史版本保留，当前版本由指针切换）：
  - **应答名单**：唯一户号 `household_id`、区县 `district`、家庭人数档
    `size_band`、正数初始权重 `init_weight`；
  - **总体目标表**：两个维度（`district` / `size_band`）下各组的目标户数
    `target_count`。
- 配权时每户权重限制在初始权重的 **0.5～2 倍**。
- 两阶段字典序优化（均为线性规划）：
  1. **先**最小化两个维度所有组「加权户数 − 目标户数」的**绝对差之和**；
  2. **再**在保持第一阶段最优的前提下，最小化各户权重相对初始权重的
     **绝对改动之和**。
- 结果分两种：
  - **能配平**（绝对差之和为 0）：下游可领取**逐户权重**；
  - **不能配平**：不提供逐户权重，只给各组
    `difference = 加权户数 − 目标户数` 的差额，供调查员排查缺样。
- 异常均带原因：缺表、缺分组目标、求解失败分别返回明确错误码与说明。
- **配权期间允许换交**：换交失败（校验不过）保留原表与既有供数；
  换交成功后旧版结果立即停供，状态回到「待配权」，进行中的旧作业完成时
  其结果因版本过时而丢弃。
- 两表尚未齐备或当前版本尚未算完时返回「待配权 / 计算中」；
  所有结果查询都**附带两表版号**。

## 补访入样

配权作业判定「无法配平」后，外业可提交**补访选户**请求，由混合整数规划
（MIP）求出能让区县与家庭人数档**同时配平**的最少候补户：

- 请求携带无法配平的**作业号**与**最多 30 户候补资料**，字段沿用应答名单
  （`household_id` / `district` / `size_band` / `init_weight`）；
- 候补户号须**互不重复且尚未入样**（不得与当前应答名单户号撞号），
  候补户所在分组必须在作业绑定的目标表中有对应目标；
- 选户模型：原户权重仍限初始值的 **0.5～2 倍**；每户候补配一个 0/1 选择
  变量，选中时其权重同样限 0.5～2 倍、未选中时贡献为 0，目标函数最小化
  选中户数，因此返回的就是**最少补访户数**方案；
- 返回**方案号、入选户号、两表版号**；无解时返回 `no_solution` 并说明原因
  （含即使全部候补入样仍无法配平的各组差额与触顶分组）；
- 两次求解（选户 MIP、并入后重算）期间都允许换交源表：方案钉住作业对应
  的两表版号，版号一旦变化，回交即被拒绝且不动正式名单。

补访结束后凭方案号回交**已应答户号**：

- 应答户集合须与入选户**完全一致**（同集合、无重复、无缺漏），且方案钉住
  的两表版号仍为当前版本，才会把这些户并入正式名单；
- 并入后按**原两阶段规则**对新名单整体重算正式权重，新名单快照与新权重
  **在同一事务内同时生效**；
- 应答户不符、源表已换版、重算出错或写入失败时一律回滚，**保留当时的正式
  名单和供数**，方案仍停留在可重试状态（计算类失败不落方案、不翻转指针）。

## 启动

```bash
docker compose up -d --build
```

- 服务：`http://localhost:8000`
- 交互式文档（Swagger）：`http://localhost:8000/docs`
- 健康检查：`GET /health`
- SQLite 数据持久化在命名卷 `weighting-data`（容器内 `/data/weighting.db`）。

本地开发（Python 3.11）：

```bash
pip install -r requirements.txt
WEIGHTING_DB_PATH=./weighting.db uvicorn app.main:app --reload
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tables/respondents` | 整份换交应答名单（JSON） |
| POST | `/tables/respondents/csv` | 整份换交应答名单（CSV 上传） |
| POST | `/tables/targets` | 整份换交总体目标表（JSON） |
| POST | `/tables/targets/csv` | 整份换交总体目标表（CSV 上传） |
| GET | `/tables/versions` | 查看两表当前版号与内容哈希 |
| POST | `/weighting/jobs` | 对当前两表版本发起配权（后台异步） |
| GET | `/weighting/status` | 查询状态（缺表/待配权/计算中/已配平/无法配平/失败） |
| GET | `/weighting/weights` | 配平后领取逐户权重（支持 `limit`/`offset`） |
| GET | `/weighting/differences` | 领取各组加权户数减目标户数的差额 |
| POST | `/supplement/plans` | 提交作业号与至多 30 户候补资料，求最少补访方案 |
| GET | `/supplement/plans` | 补访方案列表（支持 `limit`/`offset`） |
| GET | `/supplement/plans/{plan_id}` | 查询单个方案（入选户、两表版号、无解诊断） |
| POST | `/supplement/plans/{plan_id}/completion` | 补访结束回交已应答户号，并入正式名单 |

JSON 请求体形如：

```json
{ "records": [
  {"household_id": "H0001", "district": "Dongcheng",
   "size_band": "S1", "init_weight": 1.0}
]}
```

```json
{ "records": [
  {"dimension": "district", "group_name": "Dongcheng", "target_count": 6.0},
  {"dimension": "size_band", "group_name": "S1", "target_count": 10.0}
]}
```

CSV 表头见 `examples/`：

```bash
curl -X POST http://localhost:8000/tables/respondents/csv \
  -F "file=@examples/respondents.csv"
curl -X POST http://localhost:8000/tables/targets/csv \
  -F "file=@examples/targets.csv"
```

## 典型流程

```bash
# 1. 换交两表（返回各自版号）
curl -X POST localhost:8000/tables/respondents/csv -F "file=@examples/respondents.csv"
curl -X POST localhost:8000/tables/targets/csv     -F "file=@examples/targets.csv"

# 2. 发起配权
curl -X POST localhost:8000/weighting/jobs

# 3. 轮询状态
curl localhost:8000/weighting/status

# 4a. 已配平 -> 逐户权重（响应含 respondent_version / target_version）
curl localhost:8000/weighting/weights

# 4b. 无法配平 -> 权重接口返回 409，改领差额
curl localhost:8000/weighting/differences
```

### 补访入样流程

```bash
# 5. 对无法配平的作业提交候补资料（至多 30 户，字段同应答名单）
curl -X POST localhost:8000/supplement/plans -H 'Content-Type: application/json' -d '{
  "job_id": 1,
  "candidates": [
    {"household_id": "C01", "district": "Dongcheng",
     "size_band": "S1", "init_weight": 1.0}
  ]
}'
# 有解: {"plan_id": 1, "status": "solved",
#        "selected_household_ids": ["C01", ...],
#        "respondent_version": 1, "target_version": 1, ...}
# 无解: HTTP 201 + "status": "no_solution"，附 no_solution_reason 与各组诊断

# 6. 补访结束，凭方案号回交已应答户号（须与入选户完全一致）
curl -X POST localhost:8000/supplement/plans/1/completion \
  -H 'Content-Type: application/json' \
  -d '{"responded_household_ids": ["C01"]}'

# 7. 新名单与按原规则算出的正式权重同时生效
curl localhost:8000/weighting/weights
```

## 补访错误码

| HTTP / error 码 | 触发场景 |
| --- | --- |
| `404 job_not_found` | 提交的作业号不存在 |
| `409 job_not_unbalanced` | 作业不是「无法配平」状态（已配平/计算中/失败不允许补访） |
| `400 invalid_payload` | 候补超 30 户、户号重复、户号已入样、权重非正等 |
| `409 missing_group_targets` | 候补户所在区县/人数档在作业绑定的目标表中无目标 |
| `409 source_version_changed` | 求解或回交期间两表已换版，方案绑定版号不再是当前版本 |
| `404 plan_not_found` | 方案号不存在（查询或回交） |
| `409 plan_not_actionable` | 对无解/已完成方案回交，或重复并入同一方案 |
| `409 response_mismatch` | 应答户与入选户不完全一致（缺漏、多余、重复） |
| `500 supplement_solve_failed` | 选户 MIP 求解器异常（不落方案，正式名单与供数不变） |
| `500 supplement_merge_failed` | 并入后重算失败/仍无法配平/写入失败（事务回滚，供数不变） |

## 状态机与错误码

| HTTP / error 码 | 触发场景 |
| --- | --- |
| `409 missing_table` | 两表未齐备就发起配权或查询 |
| `409 missing_group_targets` | 名单中出现的区县/人数档在目标表中没有对应目标 |
| `409 weighting_in_progress` | 当前版本已有配权作业在跑，重复发起 |
| `409 not_ready` | 当前版本尚未算完（待配权/计算中），结果暂不可领 |
| `409 result_unavailable` | 无法配平时请求逐户权重，或作业计算失败 |
| `400 invalid_payload` | 换交数据校验失败（空表、重复户号/分组、权重非正、目标为负等） |

换交校验在写入前完成，失败时数据库指针不变，旧表与旧结果继续供数；
换交成功后版号前进，旧版权重/差额立即停供。

## 设计要点

- **不可变版本快照 + 当前版本指针**：每次换交写入新快照并在事务内翻转指针，
  历史版本保留可审计，失败回滚不影响在线数据。
- **结果与版本对绑定**：权重/差额按 `(respondent_version, target_version)`
  存储；后台作业落库前再次校验版本对仍为当前版本，换交导致的过期结果直接丢弃。
- **两阶段 LP（原生 HiGHS）**：用非负松弛对把绝对值线性化——
  组约束 `Σw − d⁺ + d⁻ = target`（差额即 `d⁺ − d⁻`），
  户改动用 `w − v ≤ init`、`−w − v ≤ −init`；
  第二阶段增加 `Σ(d⁺+d⁻) ≤ 第一阶段最优 + 容差` 锁定第一目标。
- 3000 户规模端到端求解约 0.1 秒。
- **补访选户 MIP（原生 HiGHS）**：变量布局 `[原户权重 w, 候补权重 u,
  0/1 选择 z]`，用 big-M 约束 `0.5·init·z ≤ u ≤ 2·init·z` 把候补权重与
  选择变量绑定，两维分组等式强制精确配平，`min Σz` 直接给出最少补访户数；
  `mip_rel_gap = 0` 保证最优性，30 个二进制变量规模下求解为毫秒级。
  不可行时回退一次普通 LP 取得「全部候补入样」的最优差额作为无解诊断。
- **方案即版本契约**：补访方案持久化作业对应的 `(respondent_version,
  target_version)` 与全部候补资料；选户求解结束、回交写入前两次校验版号，
  换版即拒。并入采用单事务：新名单快照 → 新配权作业与权重/差额 → 翻转当前
  版本指针 → 方案置 completed，任一步失败整体回滚。

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖：配平/不可配平两条结果路径、0.5～2 倍权重边界、两阶段最优性、
缺表与缺分组目标、换交失败保留供数、换交成功停供旧结果、
配权进行中换交的作业过期处理、CSV 上传与分页；补访部分覆盖最少户数 MIP
最优性、无解诊断与求解器异常、候补校验（30 户上限/重复/已入样/缺分组）、
应答户不符与换版拒绝、求解/写入失败回滚保留供数、并入后两维配平与权重
边界等共 34 个用例。
