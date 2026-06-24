"""
Unified LLM Client package
Provides a common interface to interact with various LLM providers
"""

from .unified_client import UnifiedLLMClient
from .registry import MODEL_REGISTRY

# Singleton client instance
_unified_client = None


def get_model_function(model_name, input_type="prompt"):
    """
    Helper function to get the appropriate completion function for a model

    Args:
        model_name (str): Name of the model (e.g., "claude-3-7-sonnet", "gpt-4o")
        input_type (str): Either "prompt" or "messages"

    Returns:
        function: A function that can be called with prompt text or messages list
    """
    global _unified_client
    if _unified_client is None:
        _unified_client = UnifiedLLMClient()
    return _unified_client.get_function_for_model(model_name, input_type)


# Export available models
available_models = list(MODEL_REGISTRY.keys())