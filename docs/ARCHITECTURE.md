# 实际实现架构

- SwiftUI/AppKit：三栏工作台、设置、预览、任务历史、反馈表单。
- 独立 Python supervisor：后台扫描、工作进程限流、JSONL 事件、暂停/取消、任务报告。
- 每个来源使用独立进程组；PDFium 不在多个线程间共享实例，Office 使用独立用户配置目录。
- 转换输出先落在输出盘暂存目录，通过验证后以排他重命名原子发布（macOS `renamex_np`、Linux `renameat2`、Windows `MoveFileExW`），拒绝覆盖不同内容；平台差异集中在 `engine/platform_atomic.py`。
- 原件处理前后校验身份与 SHA-256；恢复时重新核验成品，不能只依赖存在性。
- 严格 RAG 复用 `cloud_kb_pack`，派生输出与严格内容校验采用不同保真标记。
- 运行时嵌入 `.app`，路径相对应用解析；无用户机器的开发路径依赖。
- GitHub 反馈在本机组装标题和描述，通过系统浏览器打开预填 Issue。
  仓库地址只允许 `https://github.com/owner/repo`，不接受凭据、任意主机、查询串或片段。
  无 GitHub API 令牌、自动上传、自动附加日志或后台遥测。
- 版本与默认反馈仓库集中在 `config/release.json`。构建、安装包名称与运行时版本从中读取。

平台适配层：`engine/platform_atomic.py`（原子排他发布）与 `engine/platform_office.py`（渲染器查找）。Linux 引擎在容器内验证、Windows 引擎由 CI 在真实 Windows runner 上验证；Windows/Linux 的本机界面是 `chamo ui` 的浏览器页面，与命令行、macOS 原生应用共用同一个引擎与同一套请求/事件协议。

尚未实现的设计不作为当前能力：数据库式队列、完整外置盘卷身份恢复、全格式无损处理、自动更新、云端反馈代理、命名模板、目标文件体积搜索、空间预估。按页断点续跑已实现（仅 pdf-images），其他转换仍是整文件级续跑。
