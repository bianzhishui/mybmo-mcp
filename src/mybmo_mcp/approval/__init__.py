"""写操作审批。

流程固定为 ``change_preview`` → 用户确认 → ``change_execute``。
``approval_id`` 绑定 payload 哈希、账户归属与 TTL，单次使用。
"""
