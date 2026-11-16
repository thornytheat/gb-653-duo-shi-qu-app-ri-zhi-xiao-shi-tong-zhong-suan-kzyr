# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `contract_kind` | string | 否 | M3 采信契约：`legacy`（实例内请求号）或 `instance`（服务+实例+请求号） |
| `contract_version` | integer | 否 | 服务契约版本号，批次导入时绑定且不可变 |
| `identity_key` | string | 否 | M3 去重身份键，契约前缀隔离新旧规则 |
| `batch_id` | string | 否 | 批次稳定编号，默认由服务与输入摘要派生 |
| `attempt_no` | integer | 否 | 批次第几次导入尝试（首次为 1，重试/续传递增） |

### M3 原因码补充

| 原因码 | 含义 |
|---|---|
| `MISSING_INSTANCE` | instance 契约下事件缺少 `instance_id`，隔离不合并 |
| `DUPLICATE_RETRY` | 同批次重试命中已入账身份且载荷一致，未重复入账 |
| `DUPLICATE_SKIP` | 跨批次命中相同身份与载荷，未重复入账 |
| `STALE_VERSION` | 契约切换/导入携带的 `expected_version` 已过期 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
