"""profiles/qwen_profile.py —— 为 qwen（OpenAI 兼容）定制的 Harness Profile

在官方内置的 "openai" profile 之上叠加 qwen 适配（合并语义是叠加，不是替换）。
注册后，create_deep_agent 选中 openai 兼容模型时自动应用——调用点一行不用改。

⚠️ Harness Profile 需要 deepagents>=0.5.4（public beta）。先用 _capability 自检。
"""
from deepagents import HarnessProfile, register_harness_profile


# 版本化：标注"当前这套 Harness 调校是哪一版"，便于在日志/trace 里追踪与回滚
QWEN_PROFILE_VERSION = "qwen-profile-v1"


def _qwen_profile() -> HarnessProfile:
    return HarnessProfile(
        # ① 摘掉对 qwen 无用的 Anthropic 提示词缓存中间件（此 profile 的核心动作）
        #    这正是 3.1 那个"在你栈里空转"的中间件。excluded_middleware 用字符串时
        #    匹配的是【类名】（官方示例即 {"SummarizationMiddleware"} 这种写法）。
        #    ⚠️ 不能排除 FilesystemMiddleware / SubAgentMiddleware / permission 中间件（会 ValueError）。
        excluded_middleware={"AnthropicPromptCachingMiddleware"},

        # ② 给 qwen 追加适配性的提示词后缀（按你的实测调整内容）
        #    放在最后生效；这里约束输出简洁、工具调用规范——是常见的国产模型适配点。
        system_prompt_suffix=(
            "（模型适配规则）输出与产出务必简洁、严格遵循团队格式；"
            "调用工具时一次只做一件明确的事，参数完整、路径用绝对路径。"
        ),

        # ③ 针对性覆盖某个工具的描述，让 qwen 用得更准（示例：write_todos）
        tool_description_overrides={
            "write_todos": "把任务拆成有序清单并随进展更新；每条只写一个可执行步骤。",
        },
    )


def register_qwen_profiles() -> None:
    """注册 qwen 的 Harness Profile（在应用/脚本启动早期调用一次）。

    用 provider 级 key 'openai' 注册——因为 qwen 走 OpenAI 兼容接口
    （model_provider='openai'）。只想对某个具体模型生效就用 model 级 key 'openai:qwen-max'。
    """
    register_harness_profile("openai", _qwen_profile())