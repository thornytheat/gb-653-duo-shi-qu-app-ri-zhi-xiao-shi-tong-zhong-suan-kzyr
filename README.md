# 多时区应用日志小时桶重算

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 日志采信：`/api/logs/import`
- M2 时间解析：`/api/time/normalize`
- M3 事件去重：`/api/events/dedupe`（批次导入/重试/续传）、`/api/events/contracts/switch`、`/api/events/contracts/rollback`
- M4 小时分桶：`/api/buckets/rebuild`
- M5 迟到修订：`/api/watermark/advance`
- M6 批次对账：`/api/export/buckets.csv`

        ## 核心规则

        事件先归一到 UTC，DST 重叠时保留 fold；小时桶右端开，水位只能前进且迟到事件仍进入修订。

        ### M3 采信契约

        请求号曾采用实例内编号，不同实例日志会被误合并。去重身份按服务切换契约：

        - `legacy`（默认 v0）：身份为 `legacy|request_id`，旧批次重试沿用原去重身份，避免全量换规则重复入账。
        - `instance`（v1+）：身份为 `instance|service|instance_id|request_id`；新批次缺 `instance_id` 时以 `MISSING_INSTANCE` 隔离。
        - 批次在导入瞬间绑定契约版本；切换或回退只追加新版本，不改写已接收批次身份。
        - 切换/导入携带 `expected_version` 做并发保护，过期版本返回 422 `STALE_VERSION` 且无副作用。
        - 导入中断后按检查点续传；重试逐行复核但不新增 ledger 身份（`DUPLICATE_RETRY`/`DUPLICATE_SKIP`），载荷冲突记 `DUPLICATE_CONFLICT`。

        在事件去重页（`/events`）可导入 `sample_data/events_instance.jsonl`、`sample_data/events_missing_instance.jsonl`，执行契约切换/回退并核对有效事件与冲突原因。本模块不扩展时区更正（M2）与日志撤回（M1）。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18212`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
