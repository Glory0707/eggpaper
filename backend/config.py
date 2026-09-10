"""eggpaper 配置：本地 yaml，永不入库。"""
import os

import yaml

DATA_DIR = os.environ.get("EGGPAPER_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")

DEFAULTS = {
    "provider": {
        # 任意 OpenAI 兼容端点均可；留空 api_key 时进入演示模式
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
    },
    "mock": True,          # 演示模式：不调 LLM，用启发式假数据跑通全流程
    "pdf2zh": {
        "service": "google",   # pdf2zh 翻译服务名（google/bing/openai/...）
        "options": "",         # 透传给 pdf2zh CLI 的额外参数
    },
}


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def load() -> dict:
    ensure_dirs()
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    merged = DEFAULTS.copy()
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def save(cfg: dict):
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
