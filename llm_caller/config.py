
# Default timeouts and retry settings
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 30


# Client configuration for different LLM providers
CLIENT_CONFIGS = {
    "anthropic": {
        "type": "anthropic",
        "params": {
            "api_key": '',
        }
    },
    "openai": {
        "type": "openai",
        "params": {
            "api_key": "",
            "base_url": ""
        }
    },
    "azure": {
        "type": "azure_openai",
        "params": {
            "azure_endpoint": "",
            "api_key": "",
            "api_version": ""
        }
    },
    "gemini": {
        "type": "gemini",
        "params": {
            "api_key": "",
            "base_url": ""
        }
    },
    "qianfan": {
        "type": "qianfan",
        "params": {
            "ak": "",
            "sk": ""
        }
    },
    "ali": {
        "type": "openai_compatible",
        "params": {
            "api_key": "",
            "base_url": ""
        }
    },
    "tencent": {
        "type": "openai_compatible",
        "params": {
            "api_key": "",
            "base_url": ""
        }
    },
    "deepseek_native": {
        "type": "openai_compatible",
        "params": {
            "api_key": "",
            "base_url": ""
        }
    }
}

