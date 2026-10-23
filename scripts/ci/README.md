# CI 工具

- `privacy_scan.py`：扫描待推送提交、文本、文件及发布资产；关键词哈希由私有配置提供，缺失时拒绝放行。默认提交身份为 `Chamo <dev@chamo.invalid>`。
- `hooks/pre-push`：推送前调用扫描；可安装到可信、只读的工具目录。
- `review_llm.py` 与 `review.md`：供 GitHub 独立评审使用。
- `approve_pr.sh`：仅供维护者亲自审阅受保护改动后使用；开发 Agent 不得调用或自批。
- `test_*.py`：由 GitHub Actions 验证上述工具。

测试标准见 [CI_TESTING.md](../../docs/CI_TESTING.md)。这些工具不负责启动或调度开发会话。
