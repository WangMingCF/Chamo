# 需求提案

每项需求独立建目录，先公开需求和计划，再开发。已有需求见[总目录](INDEX.md)。

```text
proposals/
  INDEX.md
  _template/
  <日期-主题>/
    PR.md
    PRD.md
    proposal.json
```

- `PR.md`：问题、范围、方案摘要、关联 PR 与验证状态。
- `PRD.md`：详细需求、用户路径、边界、验收合同和回滚。
- `proposal.json`：稳定功能标识、负责人、状态、修改范围、依赖和验收映射。

复制[模板](_template/PR.md)后填写三份文件，提交需求目录并创建 Draft PR。开发前检查已有 PR 与提案，明确负责人和依赖；并发冲突先协调。总目录是导航，不能当互斥锁。

现有目录检查工具：`python3 scripts/check_proposals.py`。它只校验目录和元数据，不证明远端认领成功。已合并或废弃的提案保留历史，不删除 PRD。
