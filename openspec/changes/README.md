# 变更目录约定

```
changes/
├── README.md          ← 本文件
├── <进行中的变更>/     ← proposal.md + design.md + tasks.md
└── archive/
    └── YYYY-MM-DD-<变更名>/   ← 已完成并上线的变更，原样保留
```

## 规则

1. **`changes/` 下只放进行中的变更**。做完就移到 `archive/YYYY-MM-DD-<名字>/`，
   日期取**完成/上线**那天，不取开始那天。
2. 归档目录**内容不改**——proposal/design/tasks 保持当时的样子，
   包括未完成项（比如 `[ ]` 的 MCC 任务），这样"当时为什么没做"是可查的。
3. 归档后要把引用改过去：`openspec/project.md` 的变更表、`docs/` 里的链接。
4. 状态写在 proposal 头部的引用块里（`> **状态**：✅ 已完成 ...`），
   不靠目录位置推断。

## 当前

| 变更 | 目录 | 完成于 |
|---|---|---|
| 真实链路验证 | `archive/2026-11-11-live-validation/` | 2026-11-11 |
| 多租户落库 | `archive/2026-11-11-multi-tenant-persistence/` | 2026-11-11（MCC 子账户暂缓） |
