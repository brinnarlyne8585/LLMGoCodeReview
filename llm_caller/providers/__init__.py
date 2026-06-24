from .anthropic_client import AnthropicClient
from .gemini_client import GeminiClient
from .openai_client import OpenAIClient
from .azure_client import AzureOpenAIClient
from .qianfan_client import QianfanClient

# Map client types to their implementation classes
CLIENT_TYPE_MAP = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "openai_compatible": OpenAIClient,
    "azure_openai": AzureOpenAIClient,
    "qianfan": QianfanClient,
    "gemini": GeminiClient,
}