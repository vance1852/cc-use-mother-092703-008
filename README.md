# 重大产业项目阶段管理平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入，以及重大产业项目的阶段门控。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/project_gate/`：重大项目阶段门控（依赖条件、责任部门、有效证据、决策权限、阶段预算、紧急例外与阶段令）；
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

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入以及重大项目阶段门控流程，不访问外部网络。

## 重大项目阶段门控（project_gate）

把每个阶段建成一道「阶段门」，门内每条门槛都带依赖条件、责任部门、有效证据要求、授权依据（法规/合同条款及规则版本）和决策角色，阶段预算以资金承诺形式入账。只有**所有适用门槛**满足（适用性按项目标签判定，不适用的门槛不参与）才允许签发该阶段令：

- 证据必须来自认可来源、带文号与内容指纹（SHA-256）、在有效期内，且经核验受理后才算「有效证据」；会议纪要式的口头开工无法替代许可；
- 阶段令签发时固化当时阶段门版本与逐项评估快照（basis_sha256），**历史批准不可修改、不可重复签发**；
- 工作包越过阶段门（进入 ready/in_progress）必须有已签发阶段令；阶段顺序强制——签发第 N 阶段令时，前序各门还须按**当前**规则版本重新评估通过；
- 证据失效或政策规则换版时，通过影响分析只标记**尚未执行**的工作包；已签发阶段令保护的包标记 `protected_by_order`，不回滚，只在复工/进入下一阶段前重核；已完成/取消的包不受影响；
- 紧急例外分「门槛级」和「工作包级」，必须写明理由、作业/金额边界和到期时间；到期自动失效，看板给出到期后的恢复条件（补什么证据/资金、援引什么依据）。

项目负责人通过看板接口一次拿到：当前卡点（`current_blockers`）、已签发门在新规则下重新浮现的问题（`reopened_alerts`）、逐门授权依据与评估结果、资金承诺台账（已承诺/已拨付/可用）、紧急例外状态和每条卡点的后续恢复条件。

角色：`planner`（发布/换版阶段门、工作包）、`director`（项目负责人，签发阶段令、工作包推进、证据核验）、`risk`（风险合规，证据核验、例外授予与关闭、影响认定）、`finance`（资金承诺）、`department`（责任部门提交证据）、`auditor`（哈希链审计）。

主要接口（均经 `X-Actor-Id` 头标识操作人）：

```
POST /projects                                建立项目（含标签）
POST /gates                                   发布阶段门（门槛库，gate_order 唯一发布版）
POST /gates/{order}/revise                    规则换版（旧版整版保留，新版本号递增）
POST /projects/{pid}/packages                 登记工作包（声明依赖门槛）
POST /projects/{pid}/packages/{id}/advance    工作包状态流转（无阶段令/有效例外时 409）
POST /projects/{pid}/evidence                 责任部门提交证据
POST /projects/{pid}/evidence/{id}/review     核验受理/驳回
POST /projects/{pid}/evidence/{id}/revoke     证据失效（自动生成未执行包影响分析）
POST /projects/{pid}/commitments              登记阶段资金承诺（committed/disbursed/frozen/withdrawn）
POST /projects/{pid}/overrides                授予紧急例外（scope、target、reason、boundary、expires_at）
POST /projects/{pid}/overrides/{id}/close     提前关闭例外
GET  /projects/{pid}/gates/{order}/status     门控评估（逐项状态、卡点、资金台账）
POST /projects/{pid}/gates/{order}/issue      签发阶段令（门槛不全返回 409 gating_blocked）
POST /projects/{pid}/impacts                  认定规则换版/条件失效的影响范围
GET  /projects/{pid}/dashboard                项目负责人看板
GET  /audit/chain?project_id={pid}            哈希链审计校验
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m project_gate.api --database gate.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
