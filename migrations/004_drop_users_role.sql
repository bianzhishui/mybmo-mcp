-- 删除 users.role：它从未被任何 SQL 读写，且语义与实现的权限模型冲突
--
-- 背景（2026-11-12 排查）：绑定/分享时创建用户行走的是
-- `PgTenancyStore._user_id()`，只 INSERT (id, tenant_id, idp_subject, display_name)，
-- 于是 role 永远是建表默认值 'member' —— 全表没有一行是 owner/admin。
--
-- 为什么删而不是补值：
--   * 本服务的权限**只来自 grants**（`account_grants.permission` + 账户 owner），
--     `tenancy.py` 的文档里已经写明"含 role 之类的字段会被忽略——所有权限只来自
--     grants，没有隐含的管理员"。留一个 role 列会让人以为存在租户级 RBAC
--   * 没有语义定义：绑定时谁是 owner？没有产品结论，硬编码任何值都是猜
--
-- 将来真要做租户级角色，按当时的模型重新加列即可，成本与现在补值相同。

ALTER TABLE users DROP COLUMN IF EXISTS role;
