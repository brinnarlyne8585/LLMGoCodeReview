# Model registry - maps model names to their implementations
MODEL_REGISTRY = {

    # OpenAI models
    "openai-gpt-4o": {
        "client": "openai",
        "model_name": "gpt-4o",
        "params": {"temperature": 0,
                   "seed": 42, }
    },
    "openai-gpt-4o-mini": {
        "client": "openai",
        "model_name": "gpt-4o-mini",
        "params": {"temperature": 0,
                   "seed": 42, }
    },
    "openai-gpt-4.1": {
        "client": "openai",
        "model_name": "gpt-4.1",
        "params": {"temperature": 0,
                   "seed": 42, }
    },
    "openai-gpt-4.1-mini": {
        "client": "openai",
        "model_name": "gpt-4.1-mini",
        "params": {"temperature": 0,
                   "seed": 42, }
    },
    "openai-o3-mini": {
        "client": "openai",
        "model_name": "o3-mini",
        "params": {"reasoning_effort": "medium",
                   "seed": 42, }
    },
    "openai-o4-mini": {
        "client": "openai",
        "model_name": "o4-mini",
        "params": {"reasoning_effort": "medium",
                   "seed": 42, }
    },

    # Azure OpenAI models
    "azure-gpt-4o": {
        "client": "azure",
        "model_name": "gpt-4o",
        "params": {"temperature": 0,
                   "seed": 42, }
    },
    "azure-gpt-4o-mini": {
        "client": "azure",
        "model_name": "gpt-4o-mini",
        "params": {"temperature": 0,
                   "seed": 42, }
    },
    "azure-o3-mini": {
        "client": "azure",
        "model_name": "o3-mini",
        "params": {"temperature": 0,
                   "seed": 42, }
    },

    # Gemini
    "gemini-2.5-pro": {
        "client": "gemini",
        "model_name": "gemini-2.5-pro",
        "params": {"reasoning_effort": "medium",
                   # "seed": 42, # They don't have seed
                   "temperature": 0,}
    },
    "gemini-2.5-flash": {
        "client": "gemini",
        "model_name": "gemini-2.5-flash",
        "params": {"reasoning_effort": "medium",
                   # "seed": 42, # They don't have seed
                   "temperature": 0,}
    },

    # Claude models
    "claude-3-7-sonnet": {
        "client": "anthropic",
        "model_name": "claude-3-7-sonnet-20250219",
        "params": {"max_tokens": 1000,
                   "temperature": 0}
    },
    "claude-3-5-haiku": {
        "client": "anthropic",
        "model_name": "claude-3-5-haiku-20241022",
        "params": {"max_tokens": 1000,
                   "temperature": 0}
    },

    # Deepseek models
    "ali-deepseek-v3": {
        "client": "ali",
        "model_name": "deepseek-v3",
        "params": {"temperature": 0}
    },
    "ali-deepseek-r1": {
        "client": "ali",
        "model_name": "deepseek-r1",
        "params": {"temperature": 0,
                   "reasoning_effort": "medium"}
    },
    "ali-deepseek-v3.1": {
        "client": "ali",
        "model_name": "deepseek-v3.1",
        "params": {"temperature": 0}
    },
    "ali-deepseek-v3.2-exp": {
        "client": "ali",
        "model_name": "deepseek-v3.2-exp",
        "params": {"temperature": 0}
    },
    "ali-v3.2-thinking": {
        "client": "ali",
        "model_name": "deepseek-v3.2",
        "params": {"temperature": 0,
                   "extra_body": {"enable_thinking": True}
                   }
    },
    "ali-v3.2-noThinking": {
        "client": "ali",
        "model_name": "deepseek-v3.2",
        "params": {"temperature": 0,}
    },
    "deepseek-v3": {
        "client": "deepseek_native",
        "model_name": "deepseek-chat",
        "params": {"temperature": 0}
    },
    "tencent-deepseek-v3-0324": {
        "client": "tencent",
        "model_name": "deepseek-v3-0324",
        "params": {"temperature": 0}
    },
    "tencent-v3": {
        "client": "tencent",
        "model_name": "deepseek-v3",
        "params": {"temperature": 0}
    },
    "tencent-r1-0528": {
        "client": "tencent",
        "model_name": "deepseek-r1-0528",
        "params": {"temperature": 0,
                   "reasoning_effort": "medium"}
    },
    "tencent-r1": {
        "client": "tencent",
        "model_name": "deepseek-r1",
        "params": {"temperature": 0,
                   "reasoning_effort": "medium"}
    },
    "tencent-deepseek-v3.1-terminus": {
        "client": "tencent",
        "model_name": "deepseek-v3.1-terminus",
        "params": {"temperature": 0}
    },
    "tencent-deepseek-v3.2-exp": {
        "client": "tencent",
        "model_name": "deepseek-v3.2-exp",
        "params": {"temperature": 0}
    },

    # Qianfan Llama models
    "llama-3-8b": {
        "client": "qianfan",
        "model_name": "Meta-Llama-3-8B",
        "params": {"temperature": 0.01}
    },
    "llama-3-70b": {
        "client": "qianfan",
        "model_name": "Meta-Llama-3-70B",
        "params": {"temperature": 0.01}
    },
    "llama-3-1-8b": {
        "client": "qianfan",
        "model_name": "Meta-Llama-3.1-8B",
        "params": {"temperature": 0.01}
    },

}