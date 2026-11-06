# 改脚本之后怎么自测

`tests/test_client.py` 用标准库起一个假服务（`/health`、`/tts` 的 SSE、`/audio/tempo` 全有），
**不需要 GPU、不需要真服务，10 秒跑完**：

```bash
python tests/test_client.py          # 47 项：续跑指纹、id 冲突、manifest、--only、旧代码页编码、
                                     #      存音色同名保护、unload 409、模型配置键校验、openapi 对账、
                                     #      health 失败原因分类、doctor summary 的两个时间、
                                     #      懒加载提示 / model-load / 分段数字读法预览…
```

覆盖的都是"改一行就可能悄悄坏掉"的契约：退出码、stdout JSON 字段、`--resume` 语义、
manifest 落盘、中文在 cp936/cp1252 管道下的输出。改完脚本先跑它，再用真实整合包跑
一次 `doctor --force` 做验收——前者保证逻辑没崩，后者保证真机能出声。
