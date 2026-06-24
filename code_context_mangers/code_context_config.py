import os
from dataclasses import fields, dataclass
from typing import List

@dataclass
class ContextConfig:
    """Defines which contexts to generate and use."""
    use_neighborhood_context: bool = True  # Base context, should always be true
    use_semantic_context: bool = False
    use_similar_context: bool = False
    use_random_context: bool = False

    def get_active_context_names(self) -> List[str]:
        """Returns the names of enabled context fields."""
        active_names = []
        for f in fields(self):
            if getattr(self, f.name):
                # Convert use_xx_context to xx_context
                active_names.append(f.name.replace("use_", ""))
        return active_names

    def _get_dynamic_filepath(self, input_tasks_path: str, OUTPUT_DIR: str, version: str) -> str:
        """
        Generates a filename based on the input task file and the active contexts.
        """
        base_name = os.path.splitext(os.path.basename(input_tasks_path))[0]
        active_contexts = [name.replace('_context', '') for name in self.get_active_context_names()]
        config_str = "_".join(sorted(active_contexts))
        return f"{OUTPUT_DIR}/{base_name}_with_{config_str}({version}).csv"

    def __str__(self) -> str:
        """Return a readable summary, used by print(instance)."""
        active_contexts = self.get_active_context_names()
        if not active_contexts:
            return "ContextConfig (No active contexts)"
        return f"ContextConfig (Active: {', '.join(active_contexts)})"

