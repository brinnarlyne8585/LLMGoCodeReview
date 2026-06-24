from abc import ABC, abstractmethod
from .utils import timeout, get_completion_with_retry


class BaseLLMClient(ABC):
    """Abstract base class for all LLM clients"""

    def __init__(self, client_id, client_config):
        """
        Initialize the base LLM client

        Args:
            client_id (str): Identifier for this client
            client_config (dict): Configuration for this client
        """
        self.client_id = client_id
        self.config = client_config
        self.client = self._initialize_client()

    @abstractmethod
    def _initialize_client(self):
        """Initialize the actual client SDK/API"""
        pass

    @abstractmethod
    def call(self, model_name, input_data, is_messages=False, **kwargs):
        """Call the model with input llm_review_comment_generation"""
        pass

    def get_completion_function(self, model_name, params, input_type="prompt"):
        """
        Returns a function that can be called to get completions from this model

        Args:
            model_name (str): The name of the model to use
            params (dict): Additional parameters for the model
            input_type (str): Either "prompt" or "messages"

        Returns:
            callable: A function that takes input llm_review_comment_generation and returns completion
        """
        is_messages = (input_type == "messages")

        def completion_func(input_data):
            return self.call(model_name, input_data, is_messages, **params)

        # Wrap with retry logic
        def wrapped_completion_func(input_data):
            return get_completion_with_retry(completion_func, input_data)

        return wrapped_completion_func