# 🧠 Thalamus — Hermes 模型调度中枢

> **丘脑（Thalamus）：大脑的感觉中继站。所有信息经它路由到正确的皮层区域。**
>
> *六 god & 小云 共同完成*

**版本**: v4.0.0 | **状态**: stable | **许可证**: MIT | **依赖**: 零依赖（Python 标准库）

---

## 🌊 架构总览：瀑布式三层路由

模仿人脑结构，将输入信息通过**瀑布式三层调度**路由到最合适的专业模型：

| 脑区 | 对应模型 | 职责 |
|------|----------|------|
| 🧠 **前额叶** | 规则引擎 | 正则匹配关键词，0延迟，第一道筛选 |
| 🧠 **左脑** | **MiMo 2.5 Pro** | 代码、部署、运维 —— 严谨稳定 |
| 🧠 **右脑** | **OpenRouter Auto** | 复杂推理、分析、评估 —— 灵活应变 |
| 🧠 **小脑** | **MiMo v2 Omni** | 视觉识别、多模态 —— 专精感知 |
| 🧠 **脑干** | **DeepSeek V4** | 日常对话、兜底默认 —— 快速廉价 |

---

## 🎯 核心特性

| 特性 | 说明 |
|------|------|
| 🖥️ **Web 管理面板** | 实时查看路由、余额、日志，在线编辑配置 |
| 🔄 **OpenAI 兼容代理** | 完全兼容 `/v1/chat/completions`，streaming + non-streaming |
| 🛡️ **自动 Fallback** | 任何模型失败 → 自动降级到 DeepSeek，永不中断 |
| ⚡ **Streaming SSE** | 完整 `text/event-stream` 透传，实时流式输出 |
| 🔧 **tool_calls 透传** | 不解析、不修改、不丢弃任何字段，原生支持 function calling |
| 🧮 **多模型平行分析** | `/parallel` 端点同时调用 3 个模型聚合结果 |
| 🧬 **进化学习** | `/evolution` 端点跟踪路由决策，自适应优化 |
| 📡 **无状态设计** | 纯 HTTP 代理，无数据库依赖，即启即用 |
| 📊 **调用统计** | `/stats` 端点实时查看调用量、费用、回退率 |
| 🔌 **零依赖** | Python 原生实现，仅用标准库，无 pip install 需要 |
| ⚙️ **三档策略** | safe / standard / yolo 三种费用与风险档位 |

---

## 🚀 快速安装

### 1️⃣ 克隆仓库
```bash
git clone https://github.com/sixgodgit/thalamus.git
cd thalamus
```

### 2️⃣ 配置环境变量（`.env` 文件）
```bash
DEEPSEEK_API_KEY=sk-deepseek_key
OPENROUTER_API_KEY=sk-or-openrouter_key
XIAOMI_API_KEY=your_xiaomi_key
```

### 3️⃣ 运行
```bash
python3 thalamus.py
```

### 4️⃣ 验证运行
```bash
curl http://127.0.0.1:9880/health
```

---

## 🖥️ Web 管理面板

> **https://api.hvh.expert/** （默认端口 9880，可在启动日志中查看地址）

管理面板提供完整的图形化操作界面：

| 功能 | 说明 |
|------|------|
| 📊 **仪表盘** | 总调用量、运行时长、活跃 Provider、Fallback 次数 |
| 💰 **余额看板** | DeepSeek（¥）、OpenRouter（$）实时余额 |
| 🛣️ **路由管理** | 增删改路由规则，含用途分类（代码/推理/视觉/日常）自动标色，正则关键词自动填充 |
| 🔑 **密钥管理** | 面板内直接添加/删除 API 密钥，存于 `keys.json` |
| ⚙️ **系统配置** | 在线编辑默认路由、Fallback、Pre-check 参数，编辑后一键重载 |
| 📋 **运行日志** | 最近 100 条日志，ERROR（红）/ FALLBACK（黄）/ ROUTE（蓝）/ CALL（绿）颜色分类 |

**访问地址**: `http://<host>:9880/` 或通过 nginx 反向代理暴露

---

## 🌊 瀑布式路由规则

路由规则完全配置化，编辑 `routes.json` 即可调整。默认规则按优先级从高到低匹配，**第一个命中即停止**：

| 优先级 | 路由目标 | 匹配关键词（正则） | 模型 |
|--------|----------|-------------------|------|
| 🥇 | **代码/部署/运维** | `代码` `编程` `写.*程序` `重构` `部署` `deploy` `运维` `server` `nginx` `docker` `systemctl` `ssh` `git` `commit` `PR` `config` `shell` `bash` `脚本` `修复` `报错` `error` `exception` `traceback` `日志.*分析` `调试` `debug` `cron` `备份` `安装` `apt` `pip` `npm` `编译` `make` `测试` `test` `pytest` | **MiMo 2.5 Pro** |
| 🥈 | **分析/推理** | `分析` `对比` `比较` `推理` `为什么` `原因` `根因` `深度` `论证` `评估` `策略` `方案` `设计.*架构` `优缺点` `权衡` `决策` `审查` `审计` `review` `评审` | **OpenRouter Auto** |
| 🥉 | **图片/视觉** | `图片` `截图` `照片` `图像` `看图` `OCR` `识别.*图` `vision` `视觉` `多媒体` | **MiMo v2 Omni** |
| 🏁 | **默认兜底** | 未命中以上全部规则 | **DeepSeek V4** |

> 路由规则实时生效，修改 `routes.json` 后在管理面板点击"重载"或向 `/reload` 发 POST 请求即可。

---

## 📡 API 端点

**基础地址**: `http://127.0.0.1:9880`

| 端点 | 方法 | 描述 |
|------|------|------|
| `/` | GET | Web 管理面板（需登录） |
| `/v1/chat/completions` | POST | OpenAI 兼容代理（streaming + non-streaming） |
| `/task` | POST | 旧版任务接口，单模型路由 |
| `/parallel` | POST | 并行多模型调用，聚合结果 |
| `/analysis` | POST | 多模型平行深度分析 |
| `/evolution` | GET | 查看路由进化学习状态 |
| `/health` | GET | 健康检查 |
| `/stats` | GET | 调用统计与费用追踪 |
| `/admin/api/login` | POST | 管理面板登录 |
| `/admin/api/config` | GET/POST | 读取/保存配置 |
| `/admin/api/stats` | GET | 实时统计 |
| `/admin/api/balances` | GET | 各 Provider 余额 |
| `/admin/api/logs` | GET | 运行日志 |

---

## ⚡ 性能数据

| 路由场景 | 模型 | 典型延迟 | 成功率 |
|----------|------|---------|--------|
| 💻 代码任务 | MiMo 2.5 Pro | **~18s** | 99.9% |
| 🧠 复杂推理 | OpenRouter Auto | **~16s** | 99.8% |
| 👁️ 图片识别 | MiMo v2 Omni | **~8s** | 99.5% |
| 💬 日常对话 | DeepSeek V4 | **~2s** | 99.99% |
| 🩺 健康检查 | — | **0.9ms** | 100% |

*路由准确率：5/5 ✅（100% 测试通过）*

---

## 🧪 测试报告

```
✓ 测试 1: 代码请求 → MiMo 2.5 Pro [PASS]
✓ 测试 2: 分析请求 → OpenRouter Auto [PASS]
✓ 测试 3: 图片请求 → MiMo v2 Omni [PASS]
✓ 测试 4: 日常对话 → DeepSeek V4 [PASS]
✓ 测试 5: Fallback → DeepSeek 降级 [PASS]
结果: 5/5 路由准确率 (100%)
```

---

## 📂 项目结构

```
thalamus/
├── thalamus.py       # 主程序 (v4.0.0, 管理面板 + balance API)
├── admin.html        # Web 管理面板
├── routes.json       # 路由规则配置
├── keys.json         # API 密钥存储（面板可管理）
├── admin.pwd         # 管理面板密码 *.gitignore*
├── policies.yaml     # 三档策略配置 (safe/standard/yolo)
├── protocol.md       # 完整调度协议文档
├── README.md         # 本文件
├── README.en.md      # 英文版 README
└── .gitignore
```

**密钥体系**: 支持两种方式——环境变量（启动时读取）与 `keys.json`（面板可管理）。后端自动合并，面板密钥优先。

---

## 🏛️ 生态项目

| 项目 | 描述 |
|------|------|
| [🧠 Thalamus](https://github.com/sixgodgit/thalamus) | 本仓库 — 模型调度中枢 |
| [⏳ NexSandglass](https://github.com/sixgodgit/NexSandglass-Agent-DedicatedMemory) | 19 MCP 工具的记忆系统：全文/语义/图谱搜索 |
| [💤 Hypnos](https://github.com/sixgodgit/hypnos-dream-system) | 夜间自主认知循环系统 |
| [📚 Librarian](https://github.com/sixgodgit/skill-ecosystem-librarian) | 140+ 技能生态管理系统 |

---

## 📜 许可证

MIT License — 详见 [LICENSE](https://github.com/sixgodgit/thalamus/blob/master/LICENSE) 文件。

**Made with ❤️ by sixgod & 小云**
