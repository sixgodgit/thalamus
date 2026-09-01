#!/usr/bin/env python3
"""
Canon Auto-Sync — 让 Canon 四层体系真正接管技能生态

每周期（建议每周）运行：
  1. 读取 Canon 分类 (librarian-classification.json) + 实际使用数据 (.usage.json)
  2. 依据"中等方案"规则生成应禁用的技能清单：
     - 保留: 热气层(hot) + 使用次数>=5 的技能 + 明确核心技能
     - 禁用: 其余所有（种子库/隔离层/低频/零使用）
  3. 同步到 config.yaml 的 skills.disabled
  4. 输出变更报告（新增禁用 / 解除禁用）

用法：
  python3 canon_auto_sync.py          # 执行同步
  python3 canon_auto_sync.py --dry    # 只预览，不写入
  python3 canon_auto_sync.py --report # 只输出当前状态

依赖：PyYAML（hermes 自带）
"""

import json
import shutil
import sys
import time
from pathlib import Path

# ══════════════════════════════════════════
# 配置
# ══════════════════════════════════════════
SKILLS_DIR = Path("/root/.hermes/skills")
CONFIG_PATH = Path("/root/.hermes/config.yaml")
CLASSIFICATION = SKILLS_DIR / "autonomous-ai-agents/skill-ecosystem-librarian/references/librarian-classification.json"
USAGE_PATH = SKILLS_DIR / ".usage.json"

# 明确核心技能（即使低频也保留）——与当前手动配置保持一致
CORE_KEEP = {
    'hermes-agent', 'plan', 'writing-plans', 'brainstorming', 'subagent-driven-development',
    'requesting-code-review', 'systematic-debugging', 'model-awareness', 'thalamus',
    'webchat-model-routing', 'hermes-performance-tuning', 'canon', 'hermes-skill-curation',
    'skill-ecosystem-librarian', 'model-routing-hub', 'webchat-maintenance',
    # 开发核心
    'claude-code', 'test-driven-development', 'python-debugpy', 'codebase-health-review',
    'finishing-a-development-branch', 'using-git-worktrees', 'api-key-operations',
    'config-health-review', 'hermes-gateway-platform-setup', 'native-mcp', 'output-management',
    'conversational-discipline', 'deep-context-flow', 'expert-delegation', 'memory-system-governance',
    'nexsandglass', 'nexsandglass-memory', 'nyx', 'self-evolution-system', 'document-generation',
    'github-code-review', 'github-pr-workflow', 'github-repo-management', 'github-issues',
    'cloudflare-dns', 'cold-backup-strategy', 'remote-hermes-deployment', 'linux-server-health-check',
    'hermes-token-optimization', 'hermes-vision-configuration', 'lark-messaging', 'himalaya',
    'google-workspace', 'ocr-and-documents', 'local-ocr-text-extraction', 'windows-remote-mcp',
    'voice-integration', 'python-http-server-web-ui', 'server-monitoring-probe', 'tokenscale',
    'design-taste', 'autonomous-agent-architecture', 'browser-automation-workflow', 'dutch-legal-docs',
}

# 使用次数阈值：低于此值且非核心 → 禁用
USAGE_THRESHOLD = 5


def get_disk_skills() -> set:
    """扫描磁盘上所有技能目录名（扁平化）。"""
    names = set()
    for d in SKILLS_DIR.rglob("SKILL.md"):
        names.add(d.parent.name)
    return names


def normalize_name(name: str) -> str:
    """把 usage 里的别名/路径名归一化为磁盘目录名。"""
    # 'email:imap-reader' -> 'imap-reader'
    name = name.split(":")[-1]
    # 'software-development/open-webui-deployment' -> 'open-webui-deployment'
    name = name.split("/")[-1]
    return name.strip()


def get_usage() -> dict:
    """返回 {归一化技能名: 使用次数}。"""
    result = {}
    try:
        data = json.loads(USAGE_PATH.read_text())
        for raw_name, d in data.items():
            name = normalize_name(raw_name)
            result[name] = result.get(name, 0) + (d.get("use_count", 0) or 0) + (d.get("view_count", 0) or 0)
    except Exception as e:
        print(f"[WARN] usage 读取失败: {e}")
    return result


def get_canon_layers() -> dict:
    """返回 {技能名: 层}。"""
    result = {}
    try:
        data = json.loads(CLASSIFICATION.read_text())
        for s in data.get("skills", []):
            result[s.get("skill_name")] = s.get("librarian_layer")
    except Exception as e:
        print(f"[WARN] 分类读取失败: {e}")
    return result


def compute_keep_and_disable() -> tuple:
    """计算保留/禁用清单。返回 (keep, disable)。"""
    disk = get_disk_skills()
    usage = get_usage()
    layers = get_canon_layers()

    # 保留: 核心 + 使用>=阈值 + hot层
    keep = set()
    for s in disk:
        if s in CORE_KEEP:
            keep.add(s)
        elif usage.get(s, 0) >= USAGE_THRESHOLD:
            keep.add(s)
        elif layers.get(s) == "hot":
            keep.add(s)

    # 强制保留 canon 生态自身的技能
    for s in ['canon', 'hermes-skill-curation', 'skill-ecosystem-librarian']:
        if s in disk:
            keep.add(s)

    disable = disk - keep
    return keep, disable


def load_current_disabled() -> set:
    import yaml
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return set(cfg.get("skills", {}).get("disabled", []) or [])


def write_disabled(disable_list: list, dry: bool = False):
    import yaml
    if dry:
        return
    # 备份
    bak = f"{CONFIG_PATH}.canon.{int(time.time())}"
    shutil.copy(CONFIG_PATH, bak)
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("skills", {})["disabled"] = sorted(disable_list)
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"[OK] 已备份 → {bak}")


def main():
    dry = "--dry" in sys.argv
    report_only = "--report" in sys.argv

    keep, disable = compute_keep_and_disable()
    current = load_current_disabled()

    new_disable = disable - current          # 这次新增禁用的
    removed = current - disable              # 这次解除禁用的（原禁用但现在该保留）

    print("=" * 55)
    print("Canon Auto-Sync 技能生态同步")
    print("=" * 55)
    print(f"磁盘技能总数: {len(keep) + len(disable)}")
    print(f"  保留(常驻上下文): {len(keep)}")
    print(f"  禁用(不进索引):   {len(disable)}")
    print(f"当前 disabled 已有: {len(current)}")
    print(f"  新增禁用: {len(new_disable)}")
    print(f"  解除禁用: {len(removed)}")
    print("-" * 55)

    if report_only:
        return

    if new_disable:
        print(f"\n🆕 本次新增禁用 ({len(new_disable)}):")
        print("  " + ", ".join(sorted(new_disable)))
    if removed:
        print(f"\n♻️  本次解除禁用 ({len(removed)}):")
        print("  " + ", ".join(sorted(removed)))

    if not new_disable and not removed:
        print("\n✅ 无变化，生态已同步。")

    if dry:
        print("\n[DRY RUN] 未写入配置。加 --dry 去掉即为实际执行。")
        return

    write_disabled(list(disable), dry=dry)
    print(f"\n[OK] 已更新 skills.disabled → {len(disable)} 个技能。")
    print("提示：新配置在下次新会话生效（或重启 gateway）。")


if __name__ == "__main__":
    main()
