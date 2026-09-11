# 模型调用兼容

任务预算由 `runtime/reply/model_request_policy.py` 管理，与模型参数能力分开。官方 DeepSeek Flash 两个别名在文字信和后台任务中默认使用 high、10000 token 上限；JSON 审核保持原默认，第三方地址不套用该策略。Qwen 原有 10000 token 预算也由任务策略提供。截断响应仍拒绝完成或写入。

能力覆盖在启动和保存设置后的记忆重建中传递给 Mem0；只有记忆与回信使用相同地址、模型时才继承共享配置。独立记忆端点可在记忆配置的 `llm.provider_options` 中指定能力。适配层在调用 Mem0 SDK 前移除内部配置字段。审核 Responses 请求遵守 JSON 模式与推理强度覆盖，关闭推理扩展后不再强制采用该特殊路由。

所有 OpenAI 兼容接口共用网关传输、响应解析、重试和错误分类；模型名称不作为普通文本调用的准入名单。`runtime/reply/model_capabilities.py` 集中管理推理开关、JSON 模式、流式用量和 tool_choice 差异。

未知模型默认发送标准参数，不发送 DeepSeek/Qwen 推理扩展。已识别的 DeepSeek 和 Qwen 系列使用各自参数；Qwen 记忆提取明确关闭思考以配合非流式 JSON。保留现有 DeepSeek 官方审查 Responses 路由及已验证的记忆提取例外，不重定向其他服务商的请求。

部署者可在 `llm_config.json` 的 `provider_options.capabilities` 覆盖代理的实际能力，例如：

```json
{"provider_options":{"capabilities":{"thinking":"none","json_mode":false,"stream_usage":false,"tool_choice":true}}}
```

关闭 JSON wire mode 仅去掉服务商可选参数；业务层仍验证 JSON 内容。鉴权失败、截断输出、空响应和协议错误仍明确报错，不当作成功。能力覆盖不改变服务地址、模型或 Key。

参考：[DeepSeek 推理参数](https://api-docs.deepseek.com/guides/thinking_mode/)、[Qwen OpenAI 兼容思考参数](https://www.alibabacloud.com/help/en/model-studio/batch-inference)。兼容测试使用本地模拟接口；不等同于所有服务商实机验收。
