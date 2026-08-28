# Security policy

## 不要公开提交的内容

请不要提交以下内容：

- API key、token、密码、签名盐、私钥或真实请求头；
- 官方服务抓包、完整请求/响应日志和带签名 URL；
- 原版游戏文件、模型权重、用户信件、生成媒体或私有验收证据；
- 含用户名、盘符、真实安装目录或个人数据的日志。

如果凭据已经进入 Git、聊天、日志或共享附件，应立即撤销并重新生成，不要只删除工作树文件。

## 报告问题

公开 issue 只报告不含敏感数据的复现步骤。涉及凭据泄露、路径泄露、任意文件读写、非预期网络访问或数据删除的问题，请使用 GitHub 的 [Private vulnerability reporting](https://github.com/Ornn8/bside-olivia-community/security/advisories/new) 私密提交；如果该页面暂时不可用，请发送邮件至 `zzhiyuan717@gmail.com`。首次报告只需提供影响、受影响版本和最小复现摘要，不要附带真实密钥、私人数据或完整原始日志。

## 安全边界

默认运行只允许本地服务。官方服务、外部 provider、模型和原版资源必须由用户显式配置；代码应在缺失、篡改或来源不明时 fail closed。
