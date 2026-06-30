"""prompt_loader.py — prompts/*.yaml 系統提示詞的單一載入入口(SSOT)。

使用方式:
    import prompt_loader
    system = prompt_loader.load("analysis")   # → str

YAML 格式: 每個檔案含 `system:` 鍵,值為多行字串(YAML block literal `|`)。
lru_cache 確保同一進程只讀磁碟一次;修改 YAML 後需重啟 Python 進程才生效。
"""

from __future__ import annotations

import functools
import pathlib

import yaml

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"


@functools.lru_cache(maxsize=None)
def load(name: str) -> str:
    """載入 prompts/<name>.yaml 中的 `system` 欄位並回傳字串。"""
    path = _PROMPTS_DIR / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return str(data["system"])
