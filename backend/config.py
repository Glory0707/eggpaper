"""eggpaper 配置：本地 yaml，永不入库。"""
import os

import yaml

import appinfo

DATA_DIR = appinfo.data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")

DEFAULTS = {
    "provider": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "vision_model": "",
    },
    "ui_lang": "zh",
    "shot_save": True,
    "mock": False,
    "pdf2zh": {
        "service": "bing",
        "options": "",
        "path": "",
        "deepl_key": "",
    },
    "update": {
        "feed_url": "https://gitee.com/zhouao1207/eggpaper/raw/master/update",
        "auto_check": True,
        "cache_hours": 6,
    },
}

def ensure_dirs():
    os.makedirs(os.path.join(DATA_DIR, "papers"), exist_ok=True)

_cache = {"mtime": None, "cfg": None}

def load() -> dict:
    """带 mtime 缓存：每次 LLM 调用都要读一遍配置，文件没变就不重解析。"""
    try:
        mt = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
    except OSError:
        mt = None
    if _cache["cfg"] is not None and _cache["mtime"] == mt:
        return _cache["cfg"]
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
    _cache["mtime"] = mt
    _cache["cfg"] = merged
    return merged

def save(cfg: dict):
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    _cache["mtime"] = None          # 下次 load 强制重读，缓存不吞掉刚存的设置
