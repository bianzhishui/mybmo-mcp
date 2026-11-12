"""把凭据与应用密钥从加密文件搬到 PostgreSQL（ADR 0007 D8）。

用法::

    python scripts/migrate_credentials_to_pg.py --dry-run          # 只看会搬什么
    python scripts/migrate_credentials_to_pg.py                    # 搬 + 逐条回读校验
    python scripts/migrate_credentials_to_pg.py --verify-only      # 只校验，不写

三条铁律：

1. **不删源文件**。搬完保留一个发布周期，确认无问题再人工清理。
2. **逐条回读校验**。写完立刻 `load()` 回来跟源凭据逐字段比对，不一致就报错退出。
3. **不完全可信时不切换后端**。本脚本只管搬数据；`MYBMO_CREDENTIAL_BACKEND=pg`
   由人确认全部校验通过后再改，脚本不会替你做这个决定。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mybmo_mcp.auth.app_secrets import (
    FileAppSecretStore,
    PgAppSecretStore,
)
from mybmo_mcp.auth.models import Credential
from mybmo_mcp.auth.pg_store import PgCredentialStore
from mybmo_mcp.auth.vault import FileVault


@dataclass
class Report:
    moved: list[str]
    failed: list[tuple[str, str]]

    @property
    def ok(self) -> bool:
        return not self.failed


def diff_credentials(source: Credential, loaded: Credential) -> list[str]:
    """逐字段比对，返回不一致的字段名。空列表表示一致。

    直接比较 ``Credential`` 实例会漏掉"两边都对但字段写法不同"的情况，
    逐字段列出名字更利于排查（也更好写测试）。
    """
    fields = (
        "alias",
        "user_name",
        "user_id",
        "app_id",
        "secret_key",
        "access_token",
        "refresh_token",
        "open_id",
        "status",
        "note",
    )
    problems: list[str] = []
    for name in fields:
        if getattr(source, name) != getattr(loaded, name):
            problems.append(name)
    # 时间是浮点，落库再读回来会有微秒级精度差，只报明显偏差
    for name in ("expires_at", "refresh_expires_at"):
        a = float(getattr(source, name) or 0.0)
        b = float(getattr(loaded, name) or 0.0)
        if abs(a - b) > 1.0:
            problems.append(name)
    if (source.scope or {}) != (loaded.scope or {}):
        problems.append("scope")
    return problems


def migrate(
    *,
    data_dir: Path,
    master_key: bytes,
    dsn: str,
    tenant_id: str,
    dry_run: bool = False,
    verify_only: bool = False,
    skip_apps: bool = False,
) -> Report:
    vault = FileVault(data_dir, master_key)
    target = PgCredentialStore(dsn, master_key, tenant_id=tenant_id)

    moved: list[str] = []
    failed: list[tuple[str, str]] = []

    for alias in vault.aliases():
        try:
            source = vault.load(alias)
            if verify_only:
                loaded = target.load(alias)
            elif dry_run:
                print(f"[dry-run] 将搬运凭据 {alias}（{source.user_name or '无名'}）")
                continue
            else:
                target.save(source)
                loaded = target.load(alias)

            problems = diff_credentials(source, loaded)
            if problems:
                failed.append((alias, f"回读不一致：{', '.join(problems)}"))
            else:
                moved.append(alias)
                print(f"[ok] 凭据 {alias}")
        except Exception as exc:
            failed.append((alias, f"{type(exc).__name__}: {exc}"))
            print(f"[fail] 凭据 {alias}: {exc}")

    if not skip_apps:
        source_apps = FileAppSecretStore(data_dir, master_key)
        target_apps = PgAppSecretStore(dsn, master_key, tenant_id=tenant_id)
        for app in source_apps.list():
            try:
                if verify_only:
                    loaded_app = target_apps.get(app.app_id)
                    if loaded_app is None:
                        raise RuntimeError("目标库里没有这条应用")
                elif dry_run:
                    print(f"[dry-run] 将搬运应用 {app.app_id}")
                    continue
                else:
                    target_apps.save(app)
                    loaded_app = target_apps.get(app.app_id)
                    if loaded_app is None:
                        raise RuntimeError("写入后读不回来")

                if (
                    loaded_app.secret_key != app.secret_key
                    or loaded_app.scope != app.scope
                    or loaded_app.note != app.note
                ):
                    failed.append((app.app_id, "回读不一致（应用密钥）"))
                else:
                    moved.append(f"app:{app.app_id}")
                    print(f"[ok] 应用 {app.app_id}")
            except Exception as exc:
                failed.append((app.app_id, f"{type(exc).__name__}: {exc}"))
                print(f"[fail] 应用 {app.app_id}: {exc}")

    return Report(moved=moved, failed=failed)


def _master_key_from_env() -> bytes:
    from mybmo_mcp.config import Settings

    return Settings().master_key_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="凭据从加密文件迁移到 PostgreSQL")
    parser.add_argument("--data-dir", default="./data", help="凭据文件所在目录")
    parser.add_argument("--dsn", default="", help="目标库连接串，默认取 MYBMO_DATABASE_URL")
    parser.add_argument("--tenant", default="t_default", help="目标租户")
    parser.add_argument("--dry-run", action="store_true", help="只列出将搬运的对象")
    parser.add_argument("--verify-only", action="store_true", help="只校验库里已有的数据")
    parser.add_argument("--skip-apps", action="store_true", help="跳过应用密钥")
    args = parser.parse_args(argv)

    import os

    dsn = args.dsn or os.environ.get("MYBMO_DATABASE_URL", "")
    if not dsn:
        print("错误：没有目标库连接串（--dsn 或 MYBMO_DATABASE_URL）", file=sys.stderr)
        return 2

    report = migrate(
        data_dir=Path(args.data_dir),
        master_key=_master_key_from_env(),
        dsn=dsn,
        tenant_id=args.tenant,
        dry_run=args.dry_run,
        verify_only=args.verify_only,
        skip_apps=args.skip_apps,
    )

    print()
    print(f"成功 {len(report.moved)} 条，失败 {len(report.failed)} 条")
    for name, reason in report.failed:
        print(f"  - {name}: {reason}")
    if report.ok and not args.dry_run:
        print()
        print("下一步（确认无误后人工执行）：")
        print("  1. 设置 MYBMO_CREDENTIAL_BACKEND=pg 并重启服务")
        print("  2. 跑一遍 list_accounts / auth_status 确认账户可见")
        print("  3. 保留 data/accounts 与 data/apps 一个发布周期后再删")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
