-- 多租户：Casdoor client_id ↔ 租户绑定
--
-- 设计（见 docs/decisions/0008-tenant-per-client.md）：
--   * **不新建映射表**：一个租户只对应一个 client_id，一列 + 唯一索引即可表达 1:1
--   * client_id 全局唯一 → 同一个 Casdoor 应用不可能属于两个租户
--   * 先 nullable：现有租户需要一次运维动作补齐（mybmo-mcp tenant set-client）
--   * 首次见到未知 client_id 会自动开户（insert on conflict），所以这一列由代码写

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS client_id text;

-- 不用部分索引（WHERE client_id IS NOT NULL）：`INSERT … ON CONFLICT (client_id)`
-- 只能推断**完整**唯一索引；而 SQL 标准下唯一索引允许多个 NULL，正好满足
-- "还没绑定的租户可以有多条" 的需求。
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_client_id
    ON tenants (client_id);
