# 重大产业项目阶段管理平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景、硬件稳定性准入，以及省市合作重大产业项目的阶段门控。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/project_gate/`：重大项目阶段门控——依赖门槛、责任部门、有效证据、决策权限、阶段预算、资金承诺、工作包、紧急例外与政策换版；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m project_gate.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和重大项目阶段门控流程，不访问外部网络。

## 重大项目阶段门控（project_gate）

把用地、环评、资金、设备交付等前置条件纳入可追溯计划，杜绝“会议纪要称已开工、关键许可仍在补办”。

- **门控策略（政策规则）版本化**：每个阶段绑定适用门槛（类别、责任部门、证据类型与要求、证据有效期）和决策权限（批准角色，可带 `:层级` 限定，如 `approver:JOINT`）；策略按版本留存，阶段令永久快照签发时的规则哈希、门槛判定、资金承诺与例外。
- **阶段令签发条件**：下一阶段全部适用门槛已满足（责任部门提交了类型相符的有效证据）或经批准认定不适用（仅 `EXEMPTIBLE` 门槛，须书面留痕），或被在有效期内、范围匹配的紧急例外覆盖；且阶段预算已核定、资金承诺足额、工作包预算合计不超阶段预算、签发人具备决策权限。任一不满足即拒绝签发。
- **条件失效**：证据被宣布失效或超过有效期时，当前及未来阶段在执行的工作包立即暂停并登记影响与恢复条件；已完成阶段和历史阶段令不追溯、不抹改。
- **政策换版**：只对尚未进入的阶段和尚未执行的工作包生效；规格等价的门槛沿用，新增/变更门槛登记影响，补齐证据或认定不适用后自动闭环。
- **紧急例外**：必须列明覆盖的门槛（可再限定工作包）、理由和到期时间；到期或撤销后暂停范围内工作包，补齐门槛后方可恢复。
- **项目负责人视图** `GET /projects/{id}`：返回各阶段卡点（含责任部门、证据要求、失效原因）、决策授权依据（角色/层级/策略哈希）、资金承诺与缺口、预算、工作包状态、开放影响与恢复条件、紧急例外和历史阶段令。

主要接口（均需 `X-Actor-Id` 头；角色：coordinator/department/approver/auditor）：

```text
POST /policies                         # 发布门控策略版本
POST /projects                         # 立项（自动实例化门槛）
POST /projects/{id}/policy             # 迁移到新政策版本
GET  /projects/{id}                    # 负责人门控视图（卡点/授权/资金/恢复条件）
POST /budgets                          # 核定阶段预算
POST /commitments                      # 财政登记资金承诺
POST /commitments/{id}/withdraw        # 撤回资金承诺
POST /packages                         # 登记工作包（含依赖、责任部门、预算）
POST /packages/{id}/start|complete|resume
POST /evidences                        # 责任部门提交有效证据
POST /evidences/{id}/invalidate        # 宣布证据失效
POST /projects/{id}/gates/waive        # 认定可豁免门槛不适用
POST /projects/{id}/sweep-evidence     # 扫描证据到期
POST /exceptions                       # 签发紧急例外（范围/理由/到期时间）
POST /exceptions/{id}/revoke
POST /projects/{id}/sweep-exceptions   # 扫描例外到期
POST /projects/{id}/orders             # 签发下一阶段令（幂等键）
GET  /audit/chain                      # 校验哈希链审计
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m project_gate.api --database gate.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
