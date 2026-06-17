# Thalamus — 统一调度协议

> **实现：** `thalamus.py` (127.0.0.1:9880) | `policies.yaml` (策略配置)
> **定位：** Hermes 的模型调度中枢。代码→MiMo、推理→OpenRouter、图片→Omni、日常→DeepSeek。

---

## 一、路由决策树

```
用户提问
  │
  ├─ 正则命中 "代码|部署|运维|ssh|nginx|docker|bug|修复|重构|..."
  │    → /task → router.py → MiMo 2.5 Pro (token-plan-cn.xiaomimimo.com)
  │
  ├─ 正则命中 "分析|对比|推理|为什么|根因|评估|策略|方案|..."
  │    → /task → router.py → OpenRouter Auto (openrouter.ai)
  │
  ├─ 正则命中 "图片|截图|照片|OCR|vision|视觉|..."
  │    → /task → router.py → MiMo v2 Omni (token-plan-cn.xiaomimimo.com)
  │
  └─ 未命中
       → /task → router.py → DeepSeek V4 (api.deepseek.com)
```

**调用方式（Hermes execute_code 中）：**
```python
import urllib.request, json
req = urllib.request.Request('http://127.0.0.1:9880/task',
    data=json.dumps({"prompt": "任务内容", "policy": "standard"}).encode(),
    headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req, timeout=120)
result = json.loads(resp.read())
# result["routing"]["label"]  → 选中的模型
# result["result"]["model"]   → 实际返回 model 名
# result["result"]["content"] → 专家回复
```

**策略选择：**
| policy | max_tokens | 费用上限 | 典型场景 |
|--------|-----------|---------|---------|
| `safe` | 4096 | ¥2 | cron/自动任务 |
| `standard` | 16384 | ¥10 | 日常使用 |
| `yolo` | 65536 | ¥50 | 高风险操作 |

---

## 二、角色分工（用户设计哲学）

| 模型 | 角色 | 特点 |
|------|------|------|
| **DeepSeek** | 陪聊 | 便宜、量大、快。对话入口，绝不干重活 |
| **MiMo 2.5 Pro** | 老黄牛 | 慢、严谨、从不崩溃。代码/部署专用 |
| **OpenRouter** | 备用算力 | 国产不够时补位 |
| **MiMo v2 Omni** | 眼睛 | 图片/视觉专用 |

**核心原则：DeepSeek 做代码任务 = 让前台小姐去搬砖。MiMo 慢归慢，但从不出事。**

---

## 三、不可跳过的流程

**触发条件（任一即触发，必须走 thalamus）：**
- 代码：写代码、修bug、改文件、重构、code review、PR
- 部署：部署、上线、nginx、docker、systemd、k8s
- 运维：服务器、SSH、排查、进程、日志分析、重启服务
- 系统：修改配置、安装软件、权限、防火墙、网络
- 任何 terminal 调用超过 2 个
- 任何修改 /etc/、/usr/、systemd 的操作

**触发后流程：**
1. 加载 `skill_view(name='expert-delegation')`
2. 调用 `http://127.0.0.1:9880/task` 走 thalamus
3. 专家结果回 DeepSeek 整合后输出
4. 绝不跳过 1-3 直接自己干

---

## 四、安全约束（Qwen 审计，2026-06-14）

### A. 专家结果必须过滤
- 所有专家返回的数据经过格式校验后方可进入主会话整合
- 若包含可执行代码片段、系统命令或非预期格式，先审查或丢弃
- **禁止**将专家原始输出直接拼接不经过过滤

### B. 专家越权防护
- **Code 专家**：只能生成代码文本，不得执行系统修改
- **Reasoning 专家**：只能分析和建议，不得推导敏感决策
- **Vision 专家**：只能描述/提取文字，不得独立执行后续操作
- 任何触及系统修改边界 → 中断，移交用户确认

### C. 防死锁机制
- 单次委托深度 ≤ 2 层（禁止专家再委托专家）
- 等待用户确认超时（> 5 分钟）→ 输出"等待确认中"并释放锁

---

## 五、禁止行为（视为协议背叛）

| 行为 | 后果 |
|------|------|
| ❌ 判断为 code 任务后自己干 | 用户定性为"欺骗" |
| ❌ 用 DeepSeek 干完活说"已委托 MiMo" | 伪造委托 |
| ❌ 用 delegate_task 假装委托 | 已三方验证无法切模型 |
| ❌ 专家失败后编造结果冒充 | 用户会弃用 Hermes |
| ❌ 跳过 thalamus 直接调 API | 绕过了调度+审计 |
| ❌ 上下文污染（回复里带旧话题）| 记忆错乱 |

---

## 六、关键陷阱

| 陷阱 | 修复 |
|------|------|
| delegate_task 无法切模型（子代理继承 deepseek-chat） | 用 thalamus，不用 delegate_task |
| disabled_toolsets 全局杀 cron | 不做全局禁用。用 cron 的 enabled_toolsets 参数 |
| Hermes redaction 截断代码中的 API key | 用 `os.environ.get('XIAOMI_API_KEY')` |
| SOCKS5 代理阻断 API 调用 | NO_PROXY 含 openrouter.ai、xiaomimimo.com |
| LLM 把文本约束当"建议"绕过 | 物理约束 > 文本约束。thalamus 是物理层 |
| 上下文污染/话题漂移 | 以用户当前消息为唯一锚点 |
| 被用户抓到伪造委托 | 已发生3次。再犯=弃用 |

---

## 七、运维

```bash
# 查看状态
systemctl status thalamus

# 查看日志
tail -f /root/.hermes/logs/thalamus.log

# 查看调用统计
curl http://127.0.0.1:9880/stats

# 健康检查
curl http://127.0.0.1:9880/health

# 重启
systemctl restart thalamus
```
