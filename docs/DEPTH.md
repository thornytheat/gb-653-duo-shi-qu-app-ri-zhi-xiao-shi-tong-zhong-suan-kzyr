# 深度登记 多时区应用日志小时桶重算

        - 规模：中型
        - 核心规则：事件先归一到 UTC，DST 重叠时保留 fold；小时桶右端开，水位只能前进且迟到事件仍进入修订。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 日志采信 | 运行样例并复核证据 | `/api/logs/import` / `/logs` | `app/modules/m1_logs/core.py` `app/modules/m1_logs/service.py` | `tests/test_m1_logs.py` |
| M2 时间解析 | 运行样例并复核证据 | `/api/time/normalize` / `/time` | `app/modules/m2_time/core.py` `app/modules/m2_time/service.py` | `tests/test_m2_time.py` |
| M3 事件去重 | 运行样例并复核证据 | `/api/events/dedupe` / `/events` | `app/modules/m3_events/core.py` `app/modules/m3_events/service.py` | `tests/test_m3_events.py` |
| M4 小时分桶 | 运行样例并复核证据 | `/api/buckets/rebuild` / `/buckets` | `app/modules/m4_buckets/core.py` `app/modules/m4_buckets/service.py` | `tests/test_m4_buckets.py` |
| M5 迟到修订 | 运行样例并复核证据 | `/api/watermark/advance` / `/watermark` | `app/modules/m5_watermark/core.py` `app/modules/m5_watermark/service.py` | `tests/test_m5_watermark.py` |
| M6 批次对账 | 运行样例并复核证据 | `/api/export/buckets.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-03 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_logs/core.py` | `tests/test_m1_logs.py::test_trap_1` |
| TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_time/core.py` | `tests/test_m2_time.py::test_trap_1` |
| TRAP-07 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_events/core.py` | `tests/test_m3_events.py::test_trap_1` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_buckets/core.py` | `tests/test_m4_buckets.py::test_trap_1` |
| TRAP-11 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_watermark/core.py` | `tests/test_m5_watermark.py::test_trap_1` |
| TRAP-12 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-13 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_logs/core.py` | `tests/test_m1_logs.py::test_trap_2` |
| TRAP-14 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_time/core.py` | `tests/test_m2_time.py::test_trap_2` |
| TRAP-16 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_events/core.py` | `tests/test_m3_events.py::test_trap_2` |
| TRAP-23 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_buckets/core.py` | `tests/test_m4_buckets.py::test_trap_2` |
| TRAP-25 | 请求号实例内编号，跨实例日志误合并 | 全量换用新去重键 | 按服务切换采信契约；旧批次绑定原契约，新批次缺实例隔离 | `app/modules/m3_events/core.py` `app/modules/m3_events/state.py` | `tests/test_m3_events.py::test_legacy_batch_keeps_inprocess_identity` |
| TRAP-26 | 契约切换与导入并发 | 后写覆盖先写，重复入账 | `expected_version` 乐观并发，过期版本以 STALE_VERSION 拒绝且无副作用 | `app/modules/m3_events/state.py` | `tests/test_m3_events.py::test_stale_switch_rejected` |
| TRAP-27 | 导入中断后重试 | 从头重放导致重复入账 | 批次检查点落库，仅中断批次按 position 续传；重试整批复核但不新增 ledger | `app/modules/m3_events/core.py` | `tests/test_m3_events.py::test_interrupted_import_resumes_from_checkpoint` |
| TRAP-28 | 契约回退改写历史 | 已接收批次身份漂移 | 回退只追加新版本并移动指针，批次行的契约版本与身份键不可变 | `app/modules/m3_events/state.py` | `tests/test_m3_events.py::test_rollback_appends_version_but_never_rewrites_batch` |

## 采信契约（M3）

| 契约 | 去重身份 | 适用 | 缺 `instance_id` |
|---|---|---|---|
| `legacy`（默认，v0） | `legacy\|request_id` | 旧批次：请求号为实例内编号，重试沿用原身份 | 不要求 |
| `instance`（v1+） | `instance\|service\|instance_id\|request_id` | 新批次：按服务与实例区分请求 | `MISSING_INSTANCE` 隔离，不与任何实例合并 |

- 契约按服务版本化：切换/回退都追加新版本行，当前指针只指向最新版本；未登记服务默认 `legacy` v0。
- 批次在导入瞬间绑定契约版本与类型，持久化在 `m3_batches`；之后切换或回退不改变已接收批次身份。
- 已入账事件写入 `m3_ledger`（`(service, identity_key)` 唯一）。重试命中相同载荷记 `DUPLICATE_RETRY`/`DUPLICATE_SKIP`，载荷不同记 `DUPLICATE_CONFLICT`，均不新增入账。
- 每次尝试在 `m3_attempts` 留痕；中断批次状态为 `INTERRUPTED`，续传从检查点 `position` 继续，完成后为 `DONE`。
- 单批次守恒（首次尝试）：输入数 = 入账 + 重复 + 冲突 + 隔离。

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。

        ## 已知不覆盖

        - 生产系统连接、身份认证和外部密钥不在演示范围内。
