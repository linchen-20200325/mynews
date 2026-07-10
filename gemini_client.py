"""gemini_client.py — Gemini API 封裝的單一真相源(SSOT)。

職責:
  - API 金鑰蒐集(支援複數 key 環境變數)
  - 多 Key × 多模型逐一嘗試 + 指數退避(503 過載場景)
  - 統一 JSON 清洗(去 markdown 圍欄)、解析與截斷自動重試
  - LLM 回應後處理(normalize_dictionary)

對外 API:
  call_gemini_for_json(system_instruction, user_content) -> dict
  get_gemini_keys() -> list[str]
  clean_json_text(text) -> str
  normalize_dictionary(raw) -> list

零 Streamlit 相依;可被任何模組安全 import。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_FALLBACKS = "gemini-2.0-flash"
DEFAULT_GEMINI_MAX_TOKENS = 16384

_TRANSIENT_HINTS = ("503", "unavailable", "overloaded", "high demand", "try again",
                    "deadline", "resource_exhausted", "500", "internal error", "502", "504")


def get_gemini_keys() -> list[str]:
    """蒐集一把或多把 Gemini 金鑰(支援複數 key,呼叫失敗時可自動切換)。

    支援的環境變數:
      - GEMINI_API_KEY    單一,或以逗號/分號/換行分隔多把
      - GEMINI_API_KEYS   複數(同上分隔)
      - GEMINI_API_KEY_1 / GEMINI_API_KEY_2 ...  多把分開命名
    """
    keys: list[str] = []

    def add_many(raw: str) -> None:
        for part in re.split(r"[,;\n]+", raw or ""):
            p = part.strip()
            if p and p not in keys:
                keys.append(p)

    add_many(os.environ.get("GEMINI_API_KEY", ""))
    add_many(os.environ.get("GEMINI_API_KEYS", ""))
    for name, val in os.environ.items():
        if re.fullmatch(r"GEMINI_API_KEY_?\d+", name):
            add_many(val)
    return keys


def clean_json_text(text: str) -> str:
    """去除 markdown 圍欄,並擷取最外層的 JSON 值(物件或陣列)。"""
    text = text.strip()

    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    if text and text[0] not in "{[":
        candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if candidates:
            start = min(candidates)
            closer = "}" if text[start] == "{" else "]"
            end = text.rfind(closer)
            if end > start:
                text = text[start:end + 1]

    return text.strip()


def _build_gemini_config(types, system_instruction: str, max_tokens: int | None = None):
    """組 Gemini 生成設定:關閉 thinking、放大輸出上限,避免長 prompt 思考吃光額度。"""
    kwargs = {
        "system_instruction": system_instruction,
        "response_mime_type": "application/json",
        "temperature": 0.7,
    }
    try:
        if max_tokens is None:
            max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", str(DEFAULT_GEMINI_MAX_TOKENS)))
        kwargs["max_output_tokens"] = int(max_tokens)
    except (TypeError, ValueError):
        print(f"  警告: GEMINI_MAX_TOKENS 非有效數字({max_tokens!r}),"
              "本次不設 max_output_tokens", file=sys.stderr)
    try:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:  # noqa: BLE001
        pass
    try:
        return types.GenerateContentConfig(**kwargs)
    except TypeError:
        for k in ("thinking_config", "max_output_tokens"):
            kwargs.pop(k, None)
        return types.GenerateContentConfig(**kwargs)


def _resp_finish_info(resp) -> str:
    """從回應取出 finish_reason / 安全阻擋等診斷字串,供空回應時報錯。"""
    bits: list[str] = []
    try:
        for cand in (resp.candidates or []):
            fr = getattr(cand, "finish_reason", None)
            if fr is not None:
                bits.append(f"finish_reason={fr}")
    except Exception:  # noqa: BLE001
        pass
    try:
        fb = getattr(resp, "prompt_feedback", None)
        if fb:
            bits.append(f"prompt_feedback={fb}")
    except Exception:  # noqa: BLE001
        pass
    return "; ".join(bits) or "無候選內容"


def _is_transient(exc: Exception) -> bool:
    """是否為暫時性錯誤(模型過載/5xx/逾時)→ 值得退避重試,而非換金鑰也沒用。"""
    s = str(exc).lower()
    return any(h in s for h in _TRANSIENT_HINTS)


def _gemini_models(model: str) -> list[str]:
    """主模型 + 備援模型清單(過載時改用較不壅塞的模型);去重保序。"""
    out = [model]
    for fb in (os.environ.get("GEMINI_MODEL_FALLBACK") or DEFAULT_GEMINI_FALLBACKS).split(","):
        fb = fb.strip()
        if fb and fb not in out:
            out.append(fb)
    return out


def _gemini_generate_text(types, genai, model, keys, system_instruction, user_content, max_tokens):
    """多把金鑰×多模型逐一嘗試取得非空回應;遇暫時性過載(503)以指數退避重試整輪。"""
    config = _build_gemini_config(types, system_instruction, max_tokens)
    models = _gemini_models(model)
    try:
        max_attempts = max(1, int(os.environ.get("GEMINI_RETRIES") or 8))
    except (TypeError, ValueError):
        max_attempts = 8

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        transient = False
        for mdl in models:
            for key in keys:
                try:
                    client = genai.Client(api_key=key)
                    resp = client.models.generate_content(
                        model=mdl, contents=user_content, config=config,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    transient = transient or _is_transient(exc)
                    continue
                text = (resp.text or "").strip()
                if not text:
                    last_exc = RuntimeError(f"Gemini 回傳空內容({_resp_finish_info(resp)})")
                    continue
                return text
        if attempt + 1 < max_attempts and transient:
            wait = min(15 * 2 ** attempt, 120)  # 15→30→60→120s 上限(原 5→10→20→60)
            print(f"  Gemini 暫時性過載,{wait}s 後重試"
                  f"(第 {attempt + 2}/{max_attempts} 輪)...", file=sys.stderr)
            time.sleep(wait)
            continue
        break
    raise last_exc or RuntimeError("所有 Gemini 金鑰皆呼叫失敗")


def call_gemini_for_json(system_instruction: str, user_content: str) -> dict:
    """以 Gemini 讀取內容並回傳解析後的 JSON dict;多把金鑰會逐一嘗試。

    若輸出疑似被 token 上限截斷(JSON 解析失敗),會自動加大輸出上限再重試一次。
    保證回傳 dict:模型回頂層陣列/字串等非物件時視同解析失敗(raise),
    讓呼叫端的 data.setdefault(...) 一律安全,免得 AttributeError 被上層 except 吞成靜默降級。
    """
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    keys = get_gemini_keys()
    if not keys:
        raise RuntimeError("未設定 GEMINI_API_KEY")

    try:
        base_budget = int(os.environ.get("GEMINI_MAX_TOKENS", str(DEFAULT_GEMINI_MAX_TOKENS)))
    except (TypeError, ValueError):
        base_budget = DEFAULT_GEMINI_MAX_TOKENS
    budgets = [base_budget]
    if base_budget < 65536:
        budgets.append(65536)

    last_exc: Exception | None = None
    for i, budget in enumerate(budgets):
        try:
            text = _gemini_generate_text(
                types, genai, model, keys, system_instruction, user_content, budget
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue

        json_text = clean_json_text(text)
        try:
            parsed = json.loads(json_text, strict=False)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if i + 1 < len(budgets):
                continue
            raise ValueError(
                f"JSON 解析失敗(輸出可能被截斷,可調高 GEMINI_MAX_TOKENS):{exc}\n"
                f"--- 原始內容前 500 字 ---\n{json_text[:500]}"
            ) from exc
        if not isinstance(parsed, dict):
            last_exc = ValueError(
                f"Gemini 回傳頂層非 JSON 物件(型別 {type(parsed).__name__}),"
                "不符合結構化輸出約定"
            )
            if i + 1 < len(budgets):
                continue
            raise last_exc
        return parsed

    raise RuntimeError(f"所有 Gemini 金鑰皆呼叫失敗,最後錯誤:{last_exc}")


def normalize_dictionary(raw) -> list:
    """把模型回傳的白話文字典整理成乾淨的 [{term, explanation}] 陣列。"""
    if isinstance(raw, dict) and isinstance(raw.get("laymans_dictionary"), list):
        raw = raw["laymans_dictionary"]
    if not isinstance(raw, list):
        raise ValueError("laymans_dictionary 格式不是陣列")
    cleaned = [
        {"term": str(d.get("term", "")), "explanation": str(d.get("explanation", ""))}
        for d in raw
        if isinstance(d, dict) and d.get("term")
    ]
    if not cleaned:
        raise ValueError("laymans_dictionary 為空")
    return cleaned
