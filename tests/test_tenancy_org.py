"""组织与应用两个维度的隔离。

对应真实结构：Casdoor 里 built-in 组织只服务 dbx-mcp / casdoor-mcp，
pandabase 组织只服务 mybmo-mcp。授权表里**不允许跨组织关联权限**，
同一组织内还可以用 ``app`` 再收窄一层。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mybmo_mcp.auth.tenancy import (
    MODE_OIDC,
    AccessDenied,
    Principal,
    TenancyError,
    TenancyStore,
)

PANDA = "pandabase"
BUILTIN = "built-in"
TEST01 = "bfb7bb5c-2c51-4b0e-8872-09b62b63dd80"
ADMIN = "ccc3493b-abc5-4f8d-a73a-a2d67e711872"
APP_MYBMO = "709a8e16656a223a1ab7"
APP_OTHER = "06fa59d425b54981e537"


def write(path: Path, *, version: int = 2, users=None, grants=None) -> Path:
    payload: dict[str, object] = {"version": version, "grants": grants or []}
    if users is not None:
        payload["users"] = users
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def store(path: Path) -> TenancyStore:
    return TenancyStore(path, mode=MODE_OIDC)


def test_cross_org_grant_is_invisible(tmp_path: Path) -> None:
    """built-in 的授权不能给 pandabase 的用户用——这是本文件存在的唯一理由。"""
    path = write(
        tmp_path / "tenancy.json",
        users=[{"sub": TEST01, "org": PANDA, "display_name": "test01"}],
        grants=[{"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read"}],
    )
    tenancy = store(path)

    pandabase_user = tenancy.resolve_principal(TEST01, org=PANDA)
    # 同一个 sub 但组织对不上（令牌 owner 被改成 built-in 之类）
    builtin_view = tenancy.resolve_principal(TEST01, org=BUILTIN)

    assert tenancy.permission_for(pandabase_user, "main") == "read"
    assert tenancy.permission_for(builtin_view, "main") is None
    with pytest.raises(AccessDenied):
        tenancy.require(builtin_view, "main", "read")


def test_grant_without_org_is_rejected_in_v2(tmp_path: Path) -> None:
    path = write(tmp_path / "tenancy.json", grants=[{"sub": TEST01, "alias": "main"}])

    with pytest.raises(TenancyError, match="缺少 org"):
        store(path).permission_for(Principal(sub=TEST01, org=PANDA), "main")


def test_app_scoped_grant_only_matches_that_app(tmp_path: Path) -> None:
    path = write(
        tmp_path / "tenancy.json",
        grants=[
            {"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read", "app": APP_MYBMO},
        ],
    )
    tenancy = store(path)

    on_mybmo = tenancy.resolve_principal(TEST01, org=PANDA, app=APP_MYBMO)
    on_other = tenancy.resolve_principal(TEST01, org=PANDA, app=APP_OTHER)

    assert tenancy.permission_for(on_mybmo, "main") == "read"
    assert tenancy.permission_for(on_other, "main") is None


def test_org_level_grant_applies_to_any_app_in_that_org(tmp_path: Path) -> None:
    path = write(
        tmp_path / "tenancy.json",
        grants=[{"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read"}],
    )
    tenancy = store(path)

    for app in (APP_MYBMO, APP_OTHER, ""):
        principal = tenancy.resolve_principal(TEST01, org=PANDA, app=app)
        assert tenancy.permission_for(principal, "main") == "read"


def test_same_sub_can_have_grants_in_two_orgs_without_conflict(tmp_path: Path) -> None:
    path = write(
        tmp_path / "tenancy.json",
        grants=[
            {"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read"},
            {"org": BUILTIN, "sub": TEST01, "alias": "main", "permission": "admin"},
        ],
    )
    tenancy = store(path)

    assert tenancy.permission_for(tenancy.resolve_principal(TEST01, org=PANDA), "main") == "read"
    assert (
        tenancy.permission_for(tenancy.resolve_principal(TEST01, org=BUILTIN), "main") == "admin"
    )


def test_highest_grant_wins_when_both_org_and_app_match(tmp_path: Path) -> None:
    path = write(
        tmp_path / "tenancy.json",
        grants=[
            {"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read"},
            {"org": PANDA, "sub": TEST01, "alias": "main", "permission": "write", "app": APP_MYBMO},
        ],
    )
    tenancy = store(path)
    principal = tenancy.resolve_principal(TEST01, org=PANDA, app=APP_MYBMO)

    assert tenancy.permission_for(principal, "main") == "write"


def test_duplicate_grant_same_org_and_app_fails_loudly(tmp_path: Path) -> None:
    path = write(
        tmp_path / "tenancy.json",
        grants=[
            {"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read"},
            {"org": PANDA, "sub": TEST01, "alias": "main", "permission": "write"},
        ],
    )

    with pytest.raises(TenancyError, match="重复授权"):
        store(path).permission_for(Principal(sub=TEST01, org=PANDA), "main")


def test_users_are_indexed_by_org_and_sub(tmp_path: Path) -> None:
    path = write(
        tmp_path / "tenancy.json",
        users=[
            {"sub": TEST01, "org": PANDA, "display_name": "test01"},
            {"sub": ADMIN, "org": BUILTIN, "display_name": "管理员"},
        ],
        grants=[{"org": PANDA, "sub": TEST01, "alias": "main", "permission": "read"}],
    )
    tenancy = store(path)

    assert tenancy.resolve_principal(TEST01, org=PANDA).display_name == "test01"
    assert tenancy.resolve_principal(ADMIN, org=BUILTIN).display_name == "管理员"
    # 组织对不上时不要借用别人的展示名
    assert tenancy.resolve_principal(TEST01, org=BUILTIN).display_name == ""


def test_v1_file_is_still_accepted_without_org(tmp_path: Path) -> None:
    """老部署的 v1 文件按"单租户、不校验组织"处理，升级不至于直接停机。"""
    path = write(
        tmp_path / "tenancy.json",
        version=1,
        grants=[{"sub": TEST01, "alias": "main", "permission": "read"}],
    )
    tenancy = store(path)

    assert tenancy.permission_for(Principal(sub=TEST01, org=PANDA), "main") == "read"


def test_v1_file_cannot_be_talked_into_org_scope(tmp_path: Path) -> None:
    """v1 只有 sub 维度，改成 v2 却不写 org 就必须报错，不能默默放行。"""
    path = write(
        tmp_path / "tenancy.json",
        version=2,
        grants=[{"sub": TEST01, "alias": "main", "permission": "read"}],
    )

    with pytest.raises(TenancyError, match=r"缺少 org"):
        store(path).permission_for(Principal(sub=TEST01, org=PANDA), "main")
