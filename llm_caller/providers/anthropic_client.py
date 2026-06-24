import anthropic
from ..base import BaseLLMClient
from ..config import DEFAULT_TIMEOUT
from ..utils import timeout


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic's Claude models"""

    def _initialize_client(self):
        """Initialize the Anthropic client"""
        return anthropic.Anthropic(**self.config["params"])

    @timeout(DEFAULT_TIMEOUT)
    def call(self, model_name, input_data, is_messages=False, **kwargs):
        """
        Call Claude model with input llm_review_comment_generation

        Args:
            model_name (str): The Claude model to use
            input_data (str or list): Prompt text or messages
            is_messages (bool): If True, input_data is a list of messages
            **kwargs: Additional parameters for the API

        Returns:
            str: The model's response
        """
        if not is_messages:
            # Convert prompt to messages format
            messages = [{"role": "user",
                         "content": [
                             {
                                 "type": "text",
                                 "text": input_data
                             }
                         ]
                         }]
        else:
            # Convert messages to Claude format if needed
            messages = []
            for msg in input_data:
                if isinstance(msg["content"], str):
                    messages.append({
                        "role": msg["role"],
                        "content": [{"type": "text", "text": msg["content"]}]
                    })
                else:
                    messages.append(msg)

        message = self.client.messages.create(
            model=model_name,
            messages=messages,
            **kwargs
        )
        return {"response": message.content[0].text}
