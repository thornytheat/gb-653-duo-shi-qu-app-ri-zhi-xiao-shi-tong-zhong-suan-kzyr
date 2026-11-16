REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "MISSING_INSTANCE": "新批次缺少实例标识，已隔离",
    "DUPLICATE_RETRY": "重试命中已入账事件，未重复入账",
    "DUPLICATE_SKIP": "跨批次命中相同事件，未重复入账",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
