# Alex Qi 报价截图识别后端

这份是从旧的 `Downloads/main.py` 升级出来的版本，仍然是 FastAPI + Claude Vision。

## 原理

前端把截图作为 `multipart/form-data` 上传到 `/api/parse-screenshot`。后端把图片转成 base64，发送给 Claude Vision，让它返回 JSON。后端随后做数字清洗和公式校验，再返回给报价卡。

这会消耗 `ANTHROPIC_API_KEY` 对应账号的 Claude API 额度，不消耗 ChatGPT 对话 token。

## 部署

Railway 或 Render 都可以部署。环境变量必须设置：

```text
ANTHROPIC_API_KEY=你的 Anthropic API Key
```

可选：

```text
ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

部署后把 `quote-card-complete.html` 里的 `BACKEND_URL` 改成新的后端地址。
