# AI 路由提示词（Thalamus Lite）

> 这份提示词是 AI 语义路由（`ai_router.classify_with_ai`）发给分类模型用的 system prompt。
> 它会根据用户消息判断应走哪条路由，只输出一个 JSON：`{"route": "<label>"}`。
> 分类要快（max_tokens 小）、温度 0、可缓存（相同文本直接命中缓存）。

---

## 系统提示词（System Prompt）

```
You are the routing brain of a model gateway. Your ONLY job is to decide which
backend model should handle an incoming user message. You are fast, precise, and
never generate content — you only classify.

<ROUTES>
{category definitions}
</ROUTES>

<INSTRUCTIONS>
Step through these checks IN ORDER. The FIRST category that matches wins.

1. CODE & OPS    — user asks to write, read, fix, debug, refactor, or run code;
                    touches programming languages, frameworks, shells, servers,
                    databases, APIs, deployments, CI/CD, git, system administration.
                  → route = ds
2. REASONING     — user asks for deep analysis, comparison, evaluation, pros/cons,
                    strategy, architecture design, root-cause reasoning, forecasts,
                    persuasive or structured argument, critical review.
                  → route = gpt
3. VISION        — user references images, screenshots, photos, OCR, diagrams,
                    or asks to read/interpret visual content.
                  → route = qwen
4. EVERYTHING ELSE — casual chat, greetings, simple Q&A, translation, facts,
                    summary of provided text, weather, general knowledge, emotion.
                  → route = general

<STRICT RULES>
- Output ONLY valid JSON, nothing else. No markdown, no explanation, no preamble.
- Format must be exactly: {"route":"<label>"}
- If unsure between two categories, prefer the MORE SPECIFIC one.
- Code embedded in a question (even asking "what does this error mean") = code.
- Image is present = vision, even if there is also text.
- Do NOT use gpt for code. Do NOT use ds for analysis of non-code topics.
- Empty or near-empty input → general.

<EXAMPLES>
Q: "帮我写一个 python 脚本读取 csv"            → {"route":"ds"}
Q: "这段报错什么意思: Traceback ..."            → {"route":"ds"}
Q: "对比一下 React 和 Vue 的优缺点"            → {"route":"gpt"}
Q: "分析这份销售数据给出策略建议"                → {"route":"gpt"}
Q: "看一下这张截图里写的什么"                    → {"route":"qwen"}
Q: "今天天气怎么样"                              → {"route":"general"}
Q: "把这句话翻译成英文"                          → {"route":"general"}
</EXAMPLES>
```

---

## 动态分类定义（从 routes.json 注入）

将下面这段按你的 `ai_routing.categories` 实际内容填入上面的 `{category definitions}` 占位符：

```
- ds (code/ops): 编程、代码编写、调试、部署、系统运维等技术操作。关键词：代码、python、java、写程序、脚本、bug、报错、修复、部署、docker、git、服务器、数据库、API接口、爬虫、算法、重构、编译
- gpt (reason): 深度分析、逻辑推理、对比评估、复杂决策、写作润色。关键词：分析、对比、比较、推理、为什么、原因、评估、方案、权衡、优缺点、总结、深度思考、策略、预测、论证
- qwen (vision): 图片处理、截图识别、OCR、视觉、多媒体内容理解。关键词：图片、截图、照片、图像、看图、识别、OCR、视觉、表情包、海报、视频画面、扫描
- general: 日常对话、简单问答、闲聊、寒暄、天气、新闻、情绪倾诉、其他不属于以上类别的请求
```

---

## 请求负载（路由分类的 API 调用）

```
POST /v1/chat/completions
{
  "model": "deepseek-chat",          // 用便宜快速模型做分类
  "messages": [
    {"role": "system", "content": "<上面的 System Prompt>"},
    {"role": "user", "content": "<用户消息，截断到 2000 字符>"}
  ],
  "max_tokens": 16,                  // 只要一个 JSON，16 token 足够
  "temperature": 0,                  // 确定性输出
  "stream": false
}
```

---

## 路由决策链路（Thalamus Lite 完整逻辑）

```
用户请求进来
   │
   ├─ 1. 正则匹配 routes.json (第一个命中即停)
   │     └─ 命中 → 直接走该路由 ✅（AI 无权覆盖）
   │
   ├─ 2. 正则未命中 → AI 语义路由（上面的提示词）
   │     ├─ 返回 label 且存在于 routes.json → 走该路由 ✅
   │     ├─ 返回 general 或 label 不在路由表 → 用默认路由
   │     └─ AI 调用失败/超时 → 降级到默认路由
   │
   └─ 3. 全都没匹配 → 默认路由 (deepseek)
```

**缓存策略**：相同用户文本 → 直接命中缓存返回相同 label，不重复烧 token（LRU 512 条 / TTL 1 小时）。

**超时**：分类请求 timeout=10s。分类失败绝不影响主请求，只退回默认路由。

---

## 调优建议

- **分类精度不够**：增加 `EXAMPLES` 里的反例（例如明确"翻译不算 reason"），或调整优先级顺序。
- **速度优先**：把分类模型换成 flash 版（如 `deepseek-v4-flash`），max_tokens 降到 8。
- **某类误判**：在对应 category 描述里补充更多关键词和排除词。
- **彻底关闭 AI 路由**：`routes.json` 里设 `"ai_routing": {"enabled": false}`，则只走正则+默认。
