import os
import qianfan
from ..base import BaseLLMClient
from ..config import DEFAULT_TIMEOUT
from ..utils import timeout


class QianfanClient(BaseLLMClient):
    """Client for Qianfan LLM service"""

    def _initialize_client(self):
        """Initialize the Qianfan client"""
        # Set environment variables for Qianfan
        os.environ["QIANFAN_AK"] = self.config["params"]["ak"]
        os.environ["QIANFAN_SK"] = self.config["params"]["sk"]
        return qianfan.ChatCompletion()

    @timeout(DEFAULT_TIMEOUT)
    def call(self, model_name, input_data, is_messages=False, **kwargs):
        """
        Call Qianfan model with input llm_review_comment_generation

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

        response = self.client.do(
            model=model_name,
            messages=messages,
            **kwargs
        )

        return {"response": dict(response.body)["result"]}
