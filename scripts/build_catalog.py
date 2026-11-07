"""从能力索引 Markdown 生成 ``catalog.json``。

用法：

    uv run python scripts/build_catalog.py --source <api-index.md 路径>

默认输出到 ``src/mybmo_mcp/baidu/catalog.json``（随包分发）。
生成结果是构建产物，不要手改；要改先改源文档再重新生成。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from mybmo_mcp.baidu.transport import is_write_method, normalize_method, normalize_service

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src" / "mybmo_mcp" / "baidu" / "catalog.json"

HEADING_RE = re.compile(r"^##\s+(.+?)（(\d+)\s*个服务\s*/\s*(\d+)\s*个接口）")
BACKTICK_RE = re.compile(r"`([^`]+)`")


def parse(source: Path) -> tuple[dict, list[str], list[str]]:
    services: dict[str, dict] = {}
    groups: list[str] = []
    group = ""
    warnings: list[str] = []

    for line in source.read_text(encoding="utf-8").splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            group = heading.group(1).strip()
            groups.append(group)
            continue

        if not line.startswith("|") or "`" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue

        raw_service = BACKTICK_RE.search(cells[0])
        if raw_service is None:  # 表头或分隔行
            continue

        # "UploadClient(分片上传)" 这种带说明的名字，取括号前的部分
        service = normalize_service(re.split(r"[（(]", raw_service.group(1))[0])
        methods = BACKTICK_RE.findall(cells[2])
        if not methods:
            continue

        product = cells[3].strip()
        product_line = product.split("/")[-1] if "/" in product else None

        if service in services:
            warnings.append(f"服务重复出现，已合并：{service}（{group}）")
            entry = services[service]
        else:
            entry = services[service] = {
                "group": group,
                "product_line": product_line,
                "methods": {},
            }

        for raw_method in methods:
            method = normalize_method(raw_method)
            if method in entry["methods"]:
                warnings.append(f"方法重复：{service}/{method}")
                continue
            entry["methods"][method] = {
                "read": not is_write_method(method),
                "origin": raw_method,
            }

    return services, groups, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 catalog.json")
    parser.add_argument("--source", required=True, help="api-index.md 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    args = parser.parse_args()

    services, groups, warnings = parse(Path(args.source).expanduser())
    total_methods = sum(len(s["methods"]) for s in services.values())

    payload = {
        "meta": {
            "version": 1,
            "generated_at": date.today().isoformat(),
            "source": Path(args.source).name,
            "note": (
                "由 baiduads-sdk 源码反查 + 手写汇总，待与官方文档校验。"
                "product_line 为 null 表示该服务不走 api.baidu.com 网关。"
            ),
            "groups": groups,
            "stats": {"services": len(services), "methods": total_methods},
        },
        "services": dict(sorted(services.items())),
    }

    out = Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"写入 {out}")
    print(f"服务 {len(services)} / 接口 {total_methods} / 分组 {len(groups)}")
    for warning in warnings:
        print(f"  ! {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
