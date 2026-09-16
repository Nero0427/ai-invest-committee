"""LLM adapter for any OpenAI-compatible chat completion endpoint.

Standard library only. The provider is selected in config.json; the API key is
read from config.json first and then from the provider's environment variable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env": ("DEEPSEEK_API_KEY",),
        "note": "按量付费，中文财经语境表现好，单次分析约 ¥0.2-0.3",
    },
    "glm": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "env": ("ZHIPUAI_API_KEY", "GLM_API_KEY"),
        "note": "glm-4-flash 有免费额度，适合先跑通再换强模型",
    },
    "qwen": {
        "label": "阿里通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "env": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        "note": "新账号有免费额度，qwen-turbo 更便宜",
    },
    "moonshot": {
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "env": ("MOONSHOT_API_KEY",),
        "note": "按量付费",
    },
    "siliconflow": {
        "label": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "env": ("SILICONFLOW_API_KEY",),
        "note": "聚合多家开源模型，部分小模型免费",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env": ("OPENAI_API_KEY",),
        "note": "成本较高，且需要可访问的网络环境",
    },
    "ollama": {
        "label": "Ollama 本地模型",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "env": (),
        "note": "完全本地免费，需先安装 Ollama 并拉取模型",
    },
    "custom": {
        "label": "自定义兼容端点",
        "base_url": "",
        "model": "",
        "env": ("LLM_API_KEY",),
        "note": "任何 OpenAI 兼容接口，需自行填写 base_url 与 model",
    },
}


class LLMError(Exception):
    """Raised when the model call fails or returns unusable output."""


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model reply, tolerating code fences."""
    if not text or not text.strip():
        raise LLMError("model returned an empty response")
    s = text.strip()
    if s.startswith("```"):
        inner = s[3:]
        if inner.lstrip()[:4].lower() == "json":
            inner = inner.lstrip()[4:]
        s = inner.split("```")[0]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("no JSON object found in model reply")
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("model returned malformed JSON: %s" % exc) from exc


class LLM:
    """Thin wrapper around /chat/completions."""

    def __init__(self, cfg: dict):
        llm_cfg = (cfg or {}).get("llm") or {}
        self.provider = (llm_cfg.get("provider") or "deepseek").strip().lower()
        preset = PROVIDERS.get(self.provider) or PROVIDERS["custom"]

        self.base_url = (llm_cfg.get("base_url") or preset["base_url"] or "").rstrip("/")
        self.model = llm_cfg.get("model") or preset["model"] or ""
        self.temperature = float(llm_cfg.get("temperature", 0.7))
        self.timeout = int(llm_cfg.get("timeout", 90))
        self.max_tokens = int(llm_cfg.get("max_tokens", 1200))
        self.api_key = self._resolve_key(llm_cfg, preset)

    @staticmethod
    def _resolve_key(llm_cfg: dict, preset: dict) -> str:
        key = (llm_cfg.get("api_key") or "").strip()
        if key:
            return key
        for name in preset.get("env") or ():
            value = (os.environ.get(name) or "").strip()
            if value:
                return value
        return (os.environ.get("LLM_API_KEY") or "").strip()

    @property
    def ready(self) -> bool:
        """Local models need no key; hosted ones do."""
        if self.provider == "ollama":
            return True
        return bool(self.api_key and self.base_url and self.model)

    def describe(self) -> dict:
        preset = PROVIDERS.get(self.provider) or PROVIDERS["custom"]
        return {
            "provider": self.provider,
            "label": preset["label"],
            "model": self.model,
            "base_url": self.base_url,
            "ready": self.ready,
            "note": preset["note"],
        }

    # ------------------------------------------------------------ request --

    def _post(self, body: dict, retries: int = 2) -> dict:
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        last = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                last = LLMError("HTTP %s from %s: %s" % (exc.code, self.provider, detail))
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last
            except Exception as exc:
                last = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        raise LLMError("model request failed: %s" % last)

    def chat(self, system: str, user: str, json_mode: bool = False,
             temperature=None, max_tokens=None) -> str:
        if not self.ready:
            raise LLMError("provider %s is not configured with an API key" % self.provider)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature if temperature is None else float(temperature),
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        data = self._post(body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("unexpected response shape from %s" % self.provider) from exc

    def chat_json(self, system: str, user: str, **kwargs) -> dict:
        return extract_json(self.chat(system, user, json_mode=True, **kwargs))
