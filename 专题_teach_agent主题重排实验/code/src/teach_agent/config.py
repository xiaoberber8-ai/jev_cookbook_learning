"""全局配置：目录路径、模型、切块与检索参数，全部可用环境变量覆盖。

约定：所有路径默认落在项目根目录的 data/ 下，便于整目录备份；
敏感配置（LLM key）放在 .env 中，M1 链路不依赖它。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# ---- 目录 ----
DATA_DIR = Path(_env("TEACH_DATA_DIR", str(PROJECT_ROOT / "data")))
BOOKS_DIR = DATA_DIR / "books"
CHROMA_DIR = DATA_DIR / "chroma"
DB_PATH = DATA_DIR / "teach.db"
INBOX_DIR = Path(_env("TEACH_INBOX_DIR", str(DATA_DIR / "inbox")))
COST_LOG_PATH = Path(_env("TEACH_COST_LOG", str(DATA_DIR / "metrics" / "usage.jsonl")))
# M2.1：LangGraph 会话 checkpoint（跨进程多轮记忆）
CHECKPOINT_DB = DATA_DIR / "checkpoints.sqlite"
# M2.1：DeepAgents 文件后端的虚拟根目录（jail）；技能源在仓库 skills/，
# 启动时同步到 jail 内的 skills/，模型只能通过 jail 读到它们
AGENT_FS_ROOT = DATA_DIR / "agent_fs"
SKILLS_DIR_NAME = "skills"
SKILLS_SOURCE_DIR = PROJECT_ROOT / "skills"

# ---- Embedding ----
EMBED_MODEL = _env("TEACH_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBED_DEVICE = _env("TEACH_EMBED_DEVICE", "auto")  # auto / cuda / cpu
EMBED_BATCH_SIZE = _env_int("TEACH_EMBED_BATCH_SIZE", 8)
EMBED_MAX_LENGTH = _env_int("TEACH_EMBED_MAX_LENGTH", 8192)
# Qwen3-Embedding 官方：query 侧拼接 "Instruct: {task}\nQuery:{query}"，
# 多语言场景建议用英文指令；文档侧不加指令。
EMBED_QUERY_INSTRUCTION = _env(
    "TEACH_EMBED_QUERY_INSTRUCTION",
    "Given a student's question about a textbook, retrieve the textbook passages "
    "that answer the question",
)

# ---- 切块 ----
CHUNK_SIZE = _env_int("TEACH_CHUNK_SIZE", 700)
CHUNK_OVERLAP = _env_int("TEACH_CHUNK_OVERLAP", 80)
# 去掉公式与空白后少于该长度的块视为页眉/页脚/页码碎片，丢弃
CHUNK_MIN_CHARS = _env_int("TEACH_CHUNK_MIN_CHARS", 20)

# ---- 检索 ----
TOP_K = _env_int("TEACH_TOP_K", 6)
# Chroma 使用 cosine 空间，distance = 1 - cosine；0.42 ≈ 相似度 0.58，
# 属于较保守的初值，待黄金问答集评测后调整。
SCORE_THRESHOLD = _env_float("TEACH_SCORE_THRESHOLD", 0.42)
CHROMA_COLLECTION = _env("TEACH_CHROMA_COLLECTION", "teach_books")

# ---- 上传约束 ----
SUPPORTED_SUFFIXES = {".pdf", ".txt"}
MAX_FILE_MB = _env_int("TEACH_MAX_FILE_MB", 100)

# ---- 解析 ----
# 文字版 PDF 每页平均字符数低于该值，判定为扫描版（无文字层）
SCAN_AVG_CHARS_PER_PAGE = _env_int("TEACH_SCAN_AVG_CHARS_PER_PAGE", 50)

# ---- 答疑 LLM（M2；走 OpenAI 兼容协议，默认 DeepSeek 官方端点）----
LLM_MODEL = _env("TEACH_LLM_MODEL", "deepseek-flash")
LLM_BASE_URL = _env("TEACH_LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("TEACH_LLM_API_KEY", "").strip()
LLM_TEMPERATURE = _env_float("TEACH_LLM_TEMPERATURE", 0.3)

# ---- Tavily 网络检索（仅用于用户明确要求的书外拓展，不参与书本答疑主链路）----
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

# ---- 主题映射判定器（P1：课堂主题 → 教材小节的重排）----
# vector：仅用本地向量相似度（默认，零外部依赖）；
# jev：额外调用 Jev System One 对候选小节做 3 级 Score 判定。
MAPPING_JUDGE = _env("TEACH_MAPPING_JUDGE", "vector").casefold()
if MAPPING_JUDGE not in {"vector", "jev"}:
    MAPPING_JUDGE = "vector"
JEV_API_KEY = (os.getenv("TEACH_JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY") or "").strip()
JEV_MODEL = _env("TEACH_JEV_MODEL", "jev-latest")
JEV_BASE_URL = _env("TEACH_JEV_BASE_URL", "https://api.typesafe.ai")
JEV_TIMEOUT = _env_float("TEACH_JEV_TIMEOUT", 8.0)

# ---- 业务数据库（课程、目录、计划与学习记录）----
# 会话 checkpoint 与书籍入库元数据暂时继续使用各自的 SQLite；业务数据独立
# 落 MySQL，避免把 LangGraph 内部 checkpoint 表当作产品数据模型。
DATABASE_URL = os.getenv("TEACH_DATABASE_URL", "").strip()
DB_HOST = _env("TEACH_DB_HOST", "127.0.0.1")
DB_PORT = _env_int("TEACH_DB_PORT", 3306)
DB_USER = _env("TEACH_DB_USER", "teach_agent")
DB_PASSWORD = os.getenv("TEACH_DB_PASSWORD", "").strip()
DB_NAME = _env("TEACH_DB_NAME", "teach_agent")


def ensure_dirs() -> None:
    """确保运行时目录存在（幂等）。"""
    for directory in (DATA_DIR, BOOKS_DIR, CHROMA_DIR, INBOX_DIR):
        directory.mkdir(parents=True, exist_ok=True)
