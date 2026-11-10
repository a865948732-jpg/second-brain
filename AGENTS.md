# Second Brain 开发约定

先读 README.md，再按任务读取 docs/ARCHITECTURE.md 中对应模块。修改保持聚焦，保留其他人的工作。

- 代码标识符英文，文档与新增注释遵循现有中文说明；commit 使用 `<type>: <中文描述>`。
- 在独立临时库、合成 transcript 和 SQLite 夹具中验证。不得把测试或演示指向真实私人资料与已安装宿主配置。
- 原始证据只读，知识归档保留文件；修改数据 schema、哈希 canonicalization、锁或回执门时增加针对性回归。
- 工具授权、真实验证和业务结果分别判断；保持安全字段缺失时 HOLD，不能用模型自述伪造独立回执。
- 不提交 .local、个人 Vault、日志、账本、数据库、真实配置、凭据或 node_modules。
- 提交前检查 diff、运行对应回归与 `npm run check:release`；发布前运行完整 `npm test`。
- 不自动创建计划任务、修改模型 Provider 或扩展外部操作授权。
- 用户明确要求发布、推送或远端写入时，完成本地验证后按该范围执行。
