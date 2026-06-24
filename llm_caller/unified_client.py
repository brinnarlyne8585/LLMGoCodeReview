from .config import CLIENT_CONFIGS
from .registry import MODEL_REGISTRY
from .providers import CLIENT_TYPE_MAP


class UnifiedLLMClient:
    """
    Unified client to access all LLM providers through a common interface
    """

    def __init__(self):
        # Initialize clients dictionary
        self.clients = {}
        # Initialize all needed clients
        self._init_clients()

    def _init_clients(self):
        """Initialize all clients based on what's used in MODEL_REGISTRY"""
        # Find all unique client types needed
        unique_clients = set(model_info["client"] for model_info in MODEL_REGISTRY.values())

        # Initialize only the clients we need
        for client_id in unique_clients:
            if client_id not in CLIENT_CONFIGS:
                raise ValueError(f"Client configuration for '{client_id}' not found")
            self._init_single_client(client_id)

    def _init_single_client(self, client_id):
        """Initialize a single client based on its type"""
        config = CLIENT_CONFIGS[client_id]
        client_type = config["type"]

        if client_type not in CLIENT_TYPE_MAP:
            raise ValueError(f"Unknown client type: {client_type}")

        # Create client instance using the appropriate class
        client_class = CLIENT_TYPE_MAP[client_type]
        self.clients[client_id] = client_class(client_id, config)

    def get_function_for_model(self, model_name, input_type="prompt"):
        """
        Returns the appropriate function to call a specific model

        Args:
            model_name (str): The name of the model (e.g., "claude-3-7-sonnet")
            input_type (str): Either "prompt" or "messages"

        Returns:
            callable: A function that takes input llm_review_comment_generation and returns response
        """
        if model_name not in MODEL_REGISTRY:
            raise ValueError(f"Model {model_name} not found. Available models: {list(MODEL_REGISTRY.keys())}")

        if input_type not in ["prompt", "messages"]:
            raise ValueError("Input type must be either 'prompt' or 'messages'")

        # Get model configuration
        model_config = MODEL_REGISTRY[model_name]
        client_id = model_config["client"]
        model_name_full = model_config["model_name"]
        params = model_config["params"]

        # Get client
        client = self.clients[client_id]

        # Return function that calls this model
        return client.get_completion_function(model_name_full, params, input_type), params