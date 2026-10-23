# 需求

先检查已有 PR 与[目录](INDEX.md)，每项需求复制 `_template/PRD.md` 到独立目录并填写，再提交规格、创建 Draft PR，之后实现。同一任务有唯一执行端负责人：NAS-DSH、Mac-DSH 或 Mac-Claude。

PRD 是唯一规格来源，文件顶部 JSON 只记录 id、标题、负责人、状态、分支；详细流程、边界与验收写在正文。更新后运行 `python3 scripts/check_proposals.py --write` 生成目录。目录与 Draft PR 用于发现重复，不是跨设备锁；并发修改必须先认领协调。
