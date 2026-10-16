"""全局配置。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

# 密钥只从项目根的 .env 读取（参考 .env.example）
load_dotenv(ROOT / ".env", override=False)

DATA_DIR = ROOT / "data"
WORKSPACE_DIR = ROOT / "workspace"
SKILLS_DIR = ROOT / "skills"
CACHE_DIR = ROOT / ".cache"
TRACE_DIR = ROOT / "traces"
WEB_DIR = ROOT / "web"

# —— 虚拟文件系统：Agent 可持续操作的工作区布局 ——
#
# 为什么要分区而不是一个大 workspace：
#   ① **权限可以分级**——这一点在财务域尤其重要：
#      `vouchers/` 是出纳提的原始单据（可改），`archives/` 是已入账的凭证（只增不改），
#      两者的可写性完全不同。如果都在一个目录里，就只剩"能写/不能写"这一个粒度。
#   ② 清理策略不同——artifacts 可定期清，archives 必须按会计档案要求留存。
#   ③ 人看得懂——查账时一眼知道该去哪个目录找。
VIRTUAL_FS = {
    "vouchers": "出纳提交的原始单据（待审核，可改可撤）",
    "reports": "财务报表、分析报告等交付物",
    "artifacts": "超长工具结果的落盘位置（上下文只留摘要与路径）",
    "logs": "执行日志快照与审计片段",
    "rules": "会计政策、科目规则、内控规则的企业私有摘录",
    "memories": "跨会话经验沉淀的快照",
    "archives": "已审核入账的凭证与已结账期间（只增不改）",
}

# —— 模型 ——
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

MODEL = os.getenv("HARNESS_MODEL", "deepseek-chat")
SUBAGENT_MODEL = os.getenv("HARNESS_SUBAGENT_MODEL", "deepseek-chat")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")

# —— 运行时约束 ——
# 一次完整的「查账 → 定科目 → 审核 → 报表」实测要 20~35 轮，40 留了余量。
MAX_TURNS = int(os.getenv("HARNESS_MAX_TURNS", "40"))
CONTEXT_BUDGET_CHARS = int(os.getenv("HARNESS_CTX_BUDGET", "60000"))
TOOL_RESULT_INLINE_LIMIT = int(os.getenv("HARNESS_TOOL_INLINE", "2000"))
MAX_SUBAGENT_TURNS = int(os.getenv("HARNESS_SUBAGENT_TURNS", "8"))

# —— 财务域配置 ——
# 数据库：默认用项目内的 sqlite，克隆下来零配置即可跑。
DB_PATH = Path(os.getenv("FIN_DB_PATH", str(DATA_DIR / "finance.db")))
# 制单人 / 审核人：真实系统里来自登录态，这里用配置模拟两个角色
CASHIER = os.getenv("FIN_CASHIER", "出纳-张三")
ACCOUNTANT = os.getenv("FIN_ACCOUNTANT", "会计-李四")
# 会计期间（月度结账用）
FISCAL_YEAR = int(os.getenv("FIN_FISCAL_YEAR", "2026"))

# —— 虚拟文件系统：免问区（这个范围内随便读写，区外要人逐次授权）——
FS_ALLOWED_ROOTS = (WORKSPACE_DIR, DATA_DIR)

# —— 命令行 ——
# run_command 每次调用都走人工审批（卡片上原样显示命令），所以默认开启。
# 想彻底关掉就把 .env 里的 HARNESS_ALLOW_SHELL 设成 0。
ALLOW_SHELL = os.getenv("HARNESS_ALLOW_SHELL", "1").strip().lower() not in ("0", "false", "no")

# —— 审计日志 ——
AUDIT_LOG = CACHE_DIR / "audit.jsonl"

for _d in (WORKSPACE_DIR, CACHE_DIR, TRACE_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

for _name, _desc in VIRTUAL_FS.items():
    _p = WORKSPACE_DIR / _name
    _p.mkdir(parents=True, exist_ok=True)
    _readme = _p / "README.txt"
    if not _readme.exists():
        _readme.write_text(f"{_name}/\n{_desc}\n", encoding="utf-8")


def virtual_fs_layout() -> dict[str, str]:
    """返回虚拟文件系统布局，供文档与接口展示。"""
    return dict(VIRTUAL_FS)


class Settings:
    """旧 API 层用到的配置视图。

    保留这层是因为 `services/` 和 `api/` 是按 pydantic-settings 写的，
    换运行时不该顺手改业务代码——给一个同形状的适配器，业务层一行不动。
    """

    app_name = "finance-agent-harness"
    app_env = os.getenv("APP_ENV", "dev")
    siliconflow_api_key = SILICONFLOW_API_KEY
    siliconflow_base_url = SILICONFLOW_BASE_URL
    siliconflow_chat_model = os.getenv("SILICONFLOW_CHAT_MODEL", "Qwen/Qwen3-8B")
    siliconflow_embed_model = EMBED_MODEL
    chunk_size = 500
    chunk_overlap = 50
    chroma_dir = str(CACHE_DIR / "chroma")


_settings = Settings()


def get_settings() -> Settings:
    return _settings
