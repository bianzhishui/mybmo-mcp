"""账户归属：私有 + 显式分享。

模型只有三个要素：**用户（Casdoor sub）× 百度账户（alias）× 档位**。
组织与应用不在这里表达——本服务只对应 pandabase 的 mybmo-mcp 应用，
跨组织在鉴权层（aud 白名单 + owner 校验）就被挡住了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mybmo_mcp.auth.tenancy import (
    MODE_OIDC,
    PERMISSION_ADMIN,
    AccessDenied,
    Principal,
    TenancyError,
    TenancyStore,
)

ALICE = "alice-sub"
BOB = "bob-sub"


def write(path: Path, *, version: int = 2, users=None, accounts=None, grants=None) -> Path:
    payload: dict[str, object] = {"version": version, "grants": grants or []}
    if users is not None:
        payload["users"] = users
    if accounts is not None:
        payload["accounts"] = accounts
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def store(path: Path) -> TenancyStore:
    return TenancyStore(path, mode=MODE_OIDC)


def test_owner_has_admin_without_grant(tmp_path: Path) -> None:
    path = write(tmp_path / "t.json", accounts=[{"alias": "main", "owner": ALICE}])

    tenancy = store(path)

    assert tenancy.permission_for(Principal(sub=ALICE), "main") == PERMISSION_ADMIN
    assert tenancy.owner_of("main") == ALICE


def test_other_users_see_nothing_without_grant(tmp_path: Path) -> None:
    """别人的账户在 list_accounts 里根本不可见——不是"看到但拒绝"。"""
    path = write(tmp_path / "t.json", accounts=[{"alias": "main", "owner": ALICE}])

    tenancy = store(path)
    bob = Principal(sub=BOB)

    assert tenancy.permission_for(bob, "main") is None
    assert tenancy.visible(bob, ["main"]) == []
    with pytest.raises(AccessDenied, match="owner=alice-sub"):
        tenancy.require(bob, "main", "read")


def test_shared_grant_is_scoped_to_that_user_and_level(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.json",
        accounts=[{"alias": "main", "owner": ALICE}],
        grants=[{"sub": BOB, "alias": "main", "permission": "write"}],
    )
    tenancy = store(path)
    bob = Principal(sub=BOB)

    assert tenancy.visible(bob, ["main"]) == ["main"]
    assert tenancy.permission_for(bob, "main") == "write"
    assert tenancy.permission_for(Principal(sub="carol"), "main") is None
    # 分享只给到 write，admin 仍然是 owner 的
    with pytest.raises(AccessDenied):
        tenancy.require(bob, "main", "admin")


def test_admin_cannot_be_granted_to_others(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.json",
        accounts=[{"alias": "main", "owner": ALICE}],
        grants=[{"sub": BOB, "alias": "main", "permission": "admin"}],
    )

    with pytest.raises(TenancyError, match="admin 是账户 owner"):
        store(path).permission_for(Principal(sub=BOB), "main")


def test_unregistered_alias_is_invisible(tmp_path: Path) -> None:
    """凭据库里有文件但授权表没登记 → 不可见（fail closed）。"""
    path = write(tmp_path / "t.json", accounts=[])

    tenancy = store(path)

    assert tenancy.visible(Principal(sub=ALICE), ["main"]) == []


def test_account_requires_owner(tmp_path: Path) -> None:
    path = write(tmp_path / "t.json", accounts=[{"alias": "main"}])

    with pytest.raises(TenancyError, match="缺少 owner"):
        store(path).visible(Principal(sub=ALICE), ["main"])


def test_describe_separates_owned_and_shared(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.json",
        users=[{"sub": BOB, "display_name": "鲍勃"}],
        accounts=[
            {"alias": "main", "owner": ALICE},
            {"alias": "second", "owner": BOB},
        ],
        grants=[{"sub": BOB, "alias": "main", "permission": "read"}],
    )
    tenancy = store(path)

    admin_view = tenancy.describe(tenancy.resolve_principal(ALICE))
    bob_view = tenancy.describe(tenancy.resolve_principal(BOB))

    assert admin_view["owned"] == {"main": PERMISSION_ADMIN}
    assert admin_view["grants"] == {}
    assert bob_view["owned"] == {"second": PERMISSION_ADMIN}
    assert bob_view["grants"] == {"main": "read"}
    assert bob_view["account_model"] == "owned"
    assert bob_view["display_name"] == "鲍勃"


def test_duplicate_share_is_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.json",
        accounts=[{"alias": "main", "owner": ALICE}],
        grants=[
            {"sub": BOB, "alias": "main", "permission": "read"},
            {"sub": BOB, "alias": "main", "permission": "write"},
        ],
    )

    with pytest.raises(TenancyError, match="重复授权"):
        store(path).permission_for(Principal(sub=BOB), "main")


def test_v1_file_still_works_as_shared_model(tmp_path: Path) -> None:
    """v1 没有 accounts 段：按"服务级共享"解释，只认 grants。"""
    path = write(
        tmp_path / "t.json",
        version=1,
        grants=[{"sub": ALICE, "alias": "main", "permission": "admin"}],
    )
    tenancy = store(path)

    assert tenancy.permission_for(Principal(sub=ALICE), "main") == PERMISSION_ADMIN
    assert tenancy.describe(Principal(sub=ALICE))["account_model"] == "shared"


def test_register_account_writes_ownership_atomically(tmp_path: Path) -> None:
    path = write(tmp_path / "t.json", accounts=[])
    tenancy = store(path)

    tenancy.register_account("main", owner=ALICE, user_name="示例账户")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["accounts"] == [
        {"alias": "main", "owner": ALICE, "user_name": "示例账户", "note": ""}
    ]
    # 立即生效，不用重启
    assert tenancy.permission_for(Principal(sub=ALICE), "main") == PERMISSION_ADMIN
    assert path.stat().st_mode & 0o777 == 0o600


def test_share_then_revoke_by_overwrite(tmp_path: Path) -> None:
    path = write(tmp_path / "t.json", accounts=[{"alias": "main", "owner": ALICE}])
    tenancy = store(path)

    tenancy.share("main", sub=BOB, permission="read")
    assert tenancy.permission_for(Principal(sub=BOB), "main") == "read"

    tenancy.share("main", sub=BOB, permission="write")
    assert tenancy.permission_for(Principal(sub=BOB), "main") == "write"
    # 同一个 (sub, alias) 只会留一条，不会叠成两条
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["grants"]) == 1


def test_share_cannot_grant_admin(tmp_path: Path) -> None:
    path = write(tmp_path / "t.json", accounts=[{"alias": "main", "owner": ALICE}])

    with pytest.raises(TenancyError, match="admin 是 owner"):
        store(path).share("main", sub=BOB, permission="admin")


def test_stats_counts_without_leaking_identities(tmp_path: Path) -> None:
    """探活只报计数，不报 sub / 别名。"""
    path = write(
        tmp_path / "t.json",
        users=[{"sub": ALICE, "tenant_id": "t_default"}, {"sub": BOB, "tenant_id": "t_default"}],
        accounts=[{"alias": "main", "owner": ALICE}],
        grants=[{"sub": BOB, "alias": "main", "permission": "read"}],
    )

    stats = store(path).stats()

    assert stats == {"tenants": 1, "users": 2, "accounts": 1, "grants": 1}
    assert "alice-sub" not in str(stats)


def test_file_change_is_picked_up_without_reload(tmp_path: Path) -> None:
    """改了 tenancy.json 不重启也要生效——否则线上改授权等于白改。"""
    import os
    import time

    path = write(tmp_path / "t.json", accounts=[{"alias": "main", "owner": ALICE}])
    tenancy = store(path)
    assert tenancy.permission_for(Principal(sub=BOB), "main") is None

    write(
        tmp_path / "t.json",
        accounts=[{"alias": "main", "owner": ALICE}],
        grants=[{"sub": BOB, "alias": "main", "permission": "read"}],
    )
    # 保证 mtime 变化（某些文件系统精度较粗）
    stamp = time.time() + 1
    os.utime(path, (stamp, stamp))

    assert tenancy.permission_for(Principal(sub=BOB), "main") == "read"


def test_unregister_account_removes_owner_and_shares(tmp_path: Path) -> None:
    """解绑要连归属与分享一起清掉，否则会留下"有归属、无凭据"的幽灵账户。"""
    path = tmp_path / "tenancy.json"
    path.write_text(
        json.dumps({"version": 2, "users": [], "accounts": [], "grants": []}), encoding="utf-8"
    )
    store = TenancyStore(path, mode=MODE_OIDC)
    store.register_account("main", owner=ALICE, user_name="账户")
    store.share("main", sub=BOB, permission="read")

    assert store.unregister_account("main") is True
    assert store.owner_of("main") is None
    assert store.shares_of("main") == {}
    assert store.unregister_account("main") is False
