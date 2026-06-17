# Thalamus — Hermes 模型调度中枢

> 丘脑：大脑的感觉中继站。所有信息经它路由到正确的皮层区域。

Thalamus 是 Hermes Agent 的模型调度中枢，实现瀑布式三层路由：

```
用户提问 → Thalamus (127.0.0.1:9880)
  ├─ 代码/部署/运维 → MiMo 2.5 Pro
  ├─ 分析/推理      → OpenRouter Auto
  ├─ 图片/视觉      → MiMo v2 Omni
  └─ 日常           → DeepSeek V4
```

## 架构

```
前额叶（规则引擎）：正则匹配，0延迟
左脑：  MiMo 2.5 Pro（代码/部署）
右脑：  OpenRouter Auto（复杂推理）
小脑：  MiMo v2 Omni（视觉）
脑干：  DeepSeek V4（日常默认）
```

## 协议

详见 [protocol.md](protocol.md)

## 运行

```bash
systemctl status thalamus
curl http://127.0.0.1:9880/health
```

## 合并来源

- `expert-delegation` — 专家委托协议
- `multi-model-analysis` — 多模型分析
- `routing-self-evolution` — 路由自我进化
