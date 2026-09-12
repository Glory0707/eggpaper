"""eggpaper 配置：本地 yaml，永不入库。"""
import os

import yaml

import appinfo

# 数据目录：开发时是 backend/data；打包后是 %LOCALAPPDATA%\eggpaper\data，
# **不在安装目录里**——覆盖升级只换程序，用户的配置/文库/批注一根都不动
DATA_DIR = appinfo.data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")

DEFAULTS = {
    "provider": {
        # 任意 OpenAI 兼容端点均可；留空 api_key 时进入演示模式
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "vision_model": "",  # 视觉问答用的多模态模型，留空则该功能不可用
    },
    "mock": True,          # 演示模式：不调 LLM，用启发式假数据跑通全流程
    "pdf2zh": {
        # pdf2zh 翻译服务名（bing/google/openai/deepseek/...）。
        # 默认 bing：免费、不用 key、国内能连。**别改回 google**——translate.google.com
        # 在国内多数网络下连不通，而 pdf2zh 对连不通的服务不是报错而是死等重试，
        # 用户看到的是「翻译中」永远不动（实测 4 分钟 0 CPU、0 输出）。
        # openai/deepseek 会用「设置」里已填的那套 key 和模型（见 main._pdf2zh_env）。
        "service": "bing",
        "options": "",         # 透传给 pdf2zh CLI 的额外参数
    },
    "update": {
        # 更新源：一个静态目录的地址，里面放 latest.json 和安装包（见 tools/build_installer.py）
        # 留空 = 不检查更新（开发时就是这个状态）
        "feed_url": "",
        "auto_check": True,    # 打开软件时自动查一次（不打扰：查到了才提示）
        "cache_hours": 6,      # 同一个源多久之内不重复查
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
