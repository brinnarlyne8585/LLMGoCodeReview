from openai import OpenAI
from ..base import BaseLLMClient
from ..config import DEFAULT_TIMEOUT
from ..utils import timeout


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI and OpenAI-compatible APIs"""

    def _initialize_client(self):
        """Initialize the OpenAI client"""
        return OpenAI(**self.config["params"])

    @timeout(DEFAULT_TIMEOUT)
    def call(self, model_name, input_data, is_messages=False, **kwargs):
        """
        Call OpenAI model with input llm_review_comment_generation

        Args:
            model_name (str): The model to use
            input_data (str or list): Prompt text or messages
            is_messages (bool): If True, input_data is a list of messages
            **kwargs: Additional parameters for the API

        Returns:
            str: The model's response
        """
        if not is_messages:
            # Convert prompt to messages
            messages = [{"role": "user", "content": input_data}]
        else:
            messages = input_data


        response = self.client.chat.completions.create(
            model=model_name,
            messages=messages,
            **kwargs
        )

        return {"response": dict(response.choices[0].message)["content"]}
