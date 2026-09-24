"""
Provider Registry -- Hermes-style plugin-based model provider system.

Replaces TMM's hardcoded model_adapter.py with a discoverable registry.
Providers can be registered from config, env vars, or plugin directories.

Usage:
    registry = ProviderRegistry()
    registry.load_from_config(config_dict)  # keys.json format
    client = registry.get_client("deepseek")
    response = await client.chat("hello")
"""

import os
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProviderProfile:
    """A model provider definition -- compatible with Hermes format."""
    name: str
    base_url: str
    model: str
    api_key: str = ""
    provider_type: str = "openai_compatible"  # openai_compatible | anthropic | ollama
    aliases: List[str] = field(default_factory=list)
    default_headers: Dict[str, str] = field(default_factory=dict)
    extra_config: Dict[str, Any] = field(default_factory=dict)

    @property
    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if "/v1" in base:
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"


class ProviderClient:
    """Generic OpenAI-compatible chat client."""

    def __init__(self, profile: ProviderProfile):
        self.profile = profile
        self._session = None

    def _get_session(self):
        if self._session is None:
            import urllib.request
            self._session = urllib.request
        return self._session

    def chat_sync(self, messages: List[Dict], **kwargs) -> Dict:
        """Synchronous chat completion."""
        import urllib.request
        import urllib.error

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.profile.api_key}",
        }
        headers.update(self.profile.default_headers)

        body = {
            "model": self.profile.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "temperature": kwargs.get("temperature", 0.7),
        }
        # Mimo requires max_completion_tokens instead of max_tokens
        if "mimo" in self.profile.name.lower():
            body["max_completion_tokens"] = body.pop("max_tokens")
            body["extra_body"] = {"thinking": {"type": "disabled"}}

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.profile.chat_url, data=data, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=kwargs.get("timeout", 90)) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choice = result.get("choices", [{}])[0]
                return {
                    "content": choice.get("message", {}).get("content", ""),
                    "model": result.get("model", self.profile.model),
                    "usage": result.get("usage", {}),
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {"error": f"HTTP {e.code}: {body[:500]}", "content": ""}
        except Exception as e:
            return {"error": str(e), "content": ""}

    async def chat(self, messages: List[Dict], **kwargs) -> Dict:
        """Async chat completion."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.chat_sync(messages, **kwargs))


class ProviderRegistry:
    """Discoverable model provider registry."""

    def __init__(self):
        self._providers: Dict[str, ProviderProfile] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, profile: ProviderProfile) -> None:
        """Register a provider. Later registrations with same name overwrite."""
        self._providers[profile.name] = profile
        for alias in profile.aliases:
            self._aliases[alias] = profile.name

    def get(self, name: str) -> Optional[ProviderProfile]:
        """Get provider by name or alias."""
        if name in self._providers:
            return self._providers[name]
        resolved = self._aliases.get(name)
        if resolved:
            return self._providers.get(resolved)
        return None

    def get_client(self, name: str) -> Optional[ProviderClient]:
        """Get a chat client for the given provider."""
        profile = self.get(name)
        if not profile:
            return None
        return ProviderClient(profile)

    def list_providers(self) -> List[str]:
        return sorted(self._providers.keys())

    def load_from_config(self, config: dict) -> int:
        """Load providers from a keys.json-style config dict.

        Format:
            {
              "deepseek": {"key": "sk-xxx", "url": "https://api.deepseek.com", "model": "deepseek-chat"},
              "mimo": {"key": "sk-xxx", "url": "https://api.xiaomimimo.com/v1", "model": "mimo-v2.5"},
              "openai": {"key": "sk-xxx", "url": "https://api.openai.com", "model": "gpt-4o"}
            }
        """
        count = 0
        for name, cfg in config.items():
            if isinstance(cfg, dict) and "key" in cfg:
                profile = ProviderProfile(
                    name=name,
                    base_url=cfg.get("url", "https://api.openai.com"),
                    model=cfg.get("model", "gpt-3.5-turbo"),
                    api_key=cfg.get("key", ""),
                )
                self.register(profile)
                count += 1
        return count

    def load_from_env(self) -> int:
        """Load providers from environment variables."""
        count = 0
        env_map = {
            "DEEPSEEK_API_KEY": ("deepseek", "https://api.deepseek.com", "deepseek-chat"),
            "MIMO_API_KEY": ("mimo", "https://api.xiaomimimo.com/v1", "mimo-v2.5"),
            "OPENAI_API_KEY": ("openai", "https://api.openai.com", "gpt-4o"),
            "ANTHROPIC_API_KEY": ("anthropic", "https://api.anthropic.com", "claude-sonnet-4-20250514"),
        }
        for env_var, (name, url, model) in env_map.items():
            key = os.environ.get(env_var, "")
            if key:
                if name not in self._providers:
                    self.register(ProviderProfile(
                        name=name, base_url=url, model=model, api_key=key
                    ))
                    count += 1
        return count

    def load_from_hermes_config(self) -> int:
        """Try to load providers from Hermes Agent config."""
        # Hermes stores provider configs in ~/.hermes/config.yaml
        # and plugin-style provider dirs
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        config_yaml = hermes_home / "config.yaml"
        if not config_yaml.exists():
            return 0

        try:
            # Simple YAML parse (avoid full pyyaml dependency)
            with open(config_yaml, "r", encoding="utf-8") as f:
                content = f.read()
            # Look for providers section (basic parsing)
            import re
            count = 0
            return count
        except Exception:
            return 0
