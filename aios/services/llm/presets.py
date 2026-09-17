"""Provider presets, Chinese providers first.

A preset is *defaults*, not a constraint: base URL, example model and capability
flags are pre-filled so a user can get going in two fields, but every value
stays editable. That matters because people run these models through official
endpoints, enterprise endpoints, proxy gateways and self-hosted servers.

Model names deliberately live in ``example_models`` and are only ever used to
pre-fill a free-text box. Chinese vendors rename models often; a hard-coded list
would make the app stale within months.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .base import ProviderCapabilities

#: Wire formats AIOS knows how to speak.
API_STYLE_OPENAI = "openai_compatible"
API_STYLE_ANTHROPIC = "anthropic"
API_STYLE_GEMINI = "gemini"


@dataclass(frozen=True)
class ProviderPreset:
    """Defaults and capabilities for one vendor."""

    provider_id: str
    display_name: str
    api_style: str = API_STYLE_OPENAI
    default_base_url: str = ""
    requires_api_key: bool = True
    supports_json_mode: bool = True
    supports_streaming: bool = True
    supports_system_prompt: bool = True
    supports_usage: bool = True
    supports_temperature: bool = True
    supports_reasoning: bool = False
    #: Non-secret body fields merged into every request for this vendor.
    extra_body: dict = field(default_factory=dict)
    #: Purely illustrative - shown as a placeholder, never enforced.
    example_models: tuple[str, ...] = ()
    documentation_hint: str = ""
    #: China-first ordering for the Settings page.
    sort_order: int = 100

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_json_mode=self.supports_json_mode,
            supports_streaming=self.supports_streaming,
            supports_system_prompt=self.supports_system_prompt,
            supports_usage=self.supports_usage,
            supports_temperature=self.supports_temperature,
            supports_custom_base_url=True,
            supports_reasoning=self.supports_reasoning,
            requires_api_key=self.requires_api_key,
        )

    @property
    def example_model(self) -> str:
        return self.example_models[0] if self.example_models else ""

    @property
    def is_chinese(self) -> bool:
        return self.sort_order < 100


#: P0 - Chinese providers, then P1 - international and local.
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    preset.provider_id: preset
    for preset in (
        # ---------------- P0: China ----------------
        ProviderPreset(
            provider_id="deepseek",
            display_name="DeepSeek",
            default_base_url="https://api.deepseek.com",
            # The original script disabled the thinking trace; keep that default.
            extra_body={"thinking": {"type": "disabled"}},
            example_models=("deepseek-chat", "deepseek-reasoner"),
            documentation_hint="https://platform.deepseek.com",
            supports_reasoning=True,
            sort_order=10,
        ),
        ProviderPreset(
            provider_id="qwen",
            display_name="通义千问（阿里云百炼）",
            default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            example_models=("qwen-plus", "qwen-max", "qwen-turbo"),
            documentation_hint="https://bailian.console.aliyun.com",
            sort_order=20,
        ),
        ProviderPreset(
            provider_id="zhipu",
            display_name="智谱 GLM",
            default_base_url="https://open.bigmodel.cn/api/paas/v4",
            example_models=("glm-4-plus", "glm-4-air", "glm-4-flash"),
            documentation_hint="https://open.bigmodel.cn",
            sort_order=30,
        ),
        ProviderPreset(
            provider_id="moonshot",
            display_name="Kimi（月之暗面）",
            default_base_url="https://api.moonshot.cn/v1",
            example_models=("moonshot-v1-32k", "moonshot-v1-8k"),
            documentation_hint="https://platform.moonshot.cn",
            sort_order=40,
        ),
        ProviderPreset(
            provider_id="doubao",
            display_name="豆包（火山方舟）",
            default_base_url="https://ark.cn-beijing.volces.com/api/v3",
            example_models=("doubao-pro-32k",),
            documentation_hint=(
                "火山方舟需要填写「接入点 ID」(endpoint id) 作为模型名，"
                "而不是模型公开名称。"
            ),
            sort_order=50,
        ),
        ProviderPreset(
            provider_id="minimax",
            display_name="MiniMax",
            default_base_url="https://api.minimax.chat/v1",
            example_models=("abab6.5s-chat",),
            documentation_hint="https://platform.minimaxi.com",
            sort_order=60,
        ),
        ProviderPreset(
            provider_id="hunyuan",
            display_name="腾讯混元",
            default_base_url="https://api.hunyuan.cloud.tencent.com/v1",
            example_models=("hunyuan-turbo", "hunyuan-standard"),
            documentation_hint="https://cloud.tencent.com/product/hunyuan",
            sort_order=70,
        ),
        ProviderPreset(
            provider_id="qianfan",
            display_name="百度千帆 / 文心",
            default_base_url="https://qianfan.baidubce.com/v2",
            example_models=("ernie-4.5-turbo-128k", "ernie-speed-128k"),
            documentation_hint="https://console.bce.baidu.com/qianfan",
            sort_order=80,
        ),
        ProviderPreset(
            provider_id="siliconflow",
            display_name="硅基流动（SiliconFlow）",
            default_base_url="https://api.siliconflow.cn/v1",
            example_models=("deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"),
            documentation_hint="https://cloud.siliconflow.cn",
            sort_order=90,
        ),
        # ---------------- P1: international and local ----------------
        ProviderPreset(
            provider_id="openai",
            display_name="OpenAI",
            default_base_url="https://api.openai.com/v1",
            example_models=("gpt-4o-mini", "gpt-4o"),
            documentation_hint="https://platform.openai.com",
            sort_order=200,
        ),
        ProviderPreset(
            provider_id="anthropic",
            display_name="Anthropic Claude",
            api_style=API_STYLE_ANTHROPIC,
            default_base_url="https://api.anthropic.com/v1",
            # Anthropic has no response_format; JSON is requested in the prompt.
            supports_json_mode=False,
            example_models=("claude-sonnet-4-5", "claude-opus-4-5"),
            documentation_hint="https://console.anthropic.com",
            sort_order=210,
        ),
        ProviderPreset(
            provider_id="gemini",
            display_name="Google Gemini",
            api_style=API_STYLE_GEMINI,
            default_base_url="https://generativelanguage.googleapis.com/v1beta",
            example_models=("gemini-2.0-flash", "gemini-2.5-pro"),
            documentation_hint="https://aistudio.google.com",
            sort_order=220,
        ),
        ProviderPreset(
            provider_id="openrouter",
            display_name="OpenRouter",
            default_base_url="https://openrouter.ai/api/v1",
            example_models=("deepseek/deepseek-chat",),
            documentation_hint="https://openrouter.ai",
            sort_order=230,
        ),
        ProviderPreset(
            provider_id="ollama",
            display_name="Ollama（本地模型）",
            # Ollama exposes an OpenAI-compatible surface at /v1.
            default_base_url="http://127.0.0.1:11434/v1",
            requires_api_key=False,
            supports_usage=False,
            example_models=("qwen3:32b", "deepseek-r1:14b"),
            documentation_hint="本地运行，无需 API Key。先执行 ollama pull <模型名>。",
            sort_order=240,
        ),
        ProviderPreset(
            provider_id="local_openai",
            display_name="本地 / 自建 OpenAI 兼容服务",
            default_base_url="http://127.0.0.1:1234/v1",
            requires_api_key=False,
            supports_usage=False,
            example_models=(),
            documentation_hint="适用于 LM Studio、vLLM、Xinference 等兼容 /v1 的服务。",
            sort_order=250,
        ),
        ProviderPreset(
            provider_id="custom",
            display_name="自定义 OpenAI 兼容端点",
            default_base_url="",
            example_models=(),
            documentation_hint="任何提供 /chat/completions 的网关或代理。",
            sort_order=260,
        ),
    )
}


def get_preset(provider_id: str) -> Optional[ProviderPreset]:
    """Look up a preset, or None for an unknown id."""
    return PROVIDER_PRESETS.get((provider_id or "").strip().lower())


def preset_or_custom(provider_id: str) -> ProviderPreset:
    """A preset for any id - unknown vendors fall back to the custom OpenAI one."""
    preset = get_preset(provider_id)
    if preset is not None:
        return preset
    fallback = PROVIDER_PRESETS["custom"]
    return ProviderPreset(
        provider_id=provider_id or "custom",
        display_name=provider_id or fallback.display_name,
        api_style=fallback.api_style,
        default_base_url="",
    )


def ordered_presets() -> list[ProviderPreset]:
    """Presets in display order: Chinese providers first."""
    return sorted(PROVIDER_PRESETS.values(), key=lambda p: (p.sort_order, p.provider_id))


def preset_choices() -> list[tuple[str, str]]:
    """``(provider_id, display_name)`` pairs for a select control."""
    return [(p.provider_id, p.display_name) for p in ordered_presets()]


def chinese_presets() -> list[ProviderPreset]:
    return [p for p in ordered_presets() if p.is_chinese]


def international_presets() -> list[ProviderPreset]:
    return [p for p in ordered_presets() if not p.is_chinese]
