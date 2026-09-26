"""eggpaper 配置：本地 yaml，永不入库。"""
import os
import threading
import time

import yaml

import appinfo

_save_lock = threading.Lock()   # 两个窗口同时保存：写盘段串行，别把 yaml 写成两半

DATA_DIR = appinfo.data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")

DEFAULTS = {
    "provider": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        # deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用（官方 updates 页），
        # 现役模型名是 deepseek-flash / deepseek-v4-pro
        "model": "deepseek-flash",
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
            # 解析不动（半截文件/被改坏）：把坏文件挪开留证，别无声回默认——
            # 那样用户下次一保存，明文 key 就被空配置永久抹掉了
            try:
                os.replace(CONFIG_PATH, CONFIG_PATH + ".bad")
            except OSError:
                pass
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
    # 临时文件 + 原子换名：写到一半被杀/断电，盘上要么是旧配置要么是新配置，never 半截。
    # 临时名带纳秒后缀（固定名会在并发下互截）；换名撞上 load() 正开着目标文件时
    # Windows 会输 PermissionError——等一拍重试，别把 5xx 抛回设置页。
    with _save_lock:
        tmp = f"{CONFIG_PATH}.{time.time_ns()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        for i in range(6):
            try:
                os.replace(tmp, CONFIG_PATH)
                break
            except OSError:
                if i == 5:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
                time.sleep(0.05)
    _cache["mtime"] = None          # 下次 load 强制重读，缓存不吞掉刚存的设置
