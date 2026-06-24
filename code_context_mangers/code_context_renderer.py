import os
import pickle
from typing import Dict

import pandas as pd

from code_context_mangers.code_context_config import ContextConfig
from code_context_mangers.code_context_manager import OUTPUT_DIR, version
from code_context_mangers.code_context_orchestrator import ContextOrchestrator


class CodeContextRenderer:
    """
    Load precomputed context and render it into the prompt.
    """
    def __init__(self,
                 tasks_file_path: str,
                 config: ContextConfig,
                 render_mode: str = "sectioned",
                 version: str=version,
                 ):
        print(f"CodeContextRenderer: {version}")
        self.config = config or ContextConfig()
        self.render_mode = render_mode
        self.augmented_context_path: str = self.config._get_dynamic_filepath(input_tasks_path = tasks_file_path,
                                                                             OUTPUT_DIR=OUTPUT_DIR,
                                                                             version=version)
        self._context_map: Dict[str, Dict[str, str]] = {}

        # Load during initialization.
        self._load_augmented_context()

    def _load_augmented_context(self):
        """Loads pre-computed context data based on the manager's config."""
        if not os.path.exists(self.augmented_context_path):
            print(f"Warning: Context file for this config does not exist: {self.augmented_context_path}")
            return

        print(f"Loading precomputed augmented context from {self.augmented_context_path}...")
        try:
            df = pd.read_csv(self.augmented_context_path)

            # Dynamically check for required columns based on config
            required_cols = set(ContextOrchestrator.active_column_names(self.config)) | {"comment_url"}
            if not required_cols.issubset(df.columns):
                missing = required_cols - set(df.columns)
                raise ValueError(f"CSV file is missing required columns for this config: {missing}")

            df = df.drop_duplicates(subset="comment_url", keep="first")

            active_context_names = ContextOrchestrator.active_column_names(self.config)
            for col in active_context_names:
                if col in df.columns:
                    df[col] = df[col].fillna("").astype(str)

            for _, row in df.iterrows():
                row_data = {
                    name: row.get(name, "") for name in active_context_names
                }
                self._context_map[row["comment_url"]] = row_data
            print(f"Loaded {len(self._context_map)} unique context records.")
        except Exception as e:
            print(f"Error reading or parsing augmented context file: {e}")
            self._context_map = {}

    def render_file_content(
        self,
        comment_url: str,
    ) -> str:
        """Renders the final code context prompt based on available data."""
        item = self._context_map.get(comment_url, {})
        if not item:
            print(f"Warning: No context found for comment_url '{comment_url}'.")
            return "Context not available."

        if self.render_mode == "random":
            return self._render_random_prompt(item.get("random_context", ""))
        if self.render_mode != "sectioned":
            raise ValueError(f"Unknown render_mode: {self.render_mode}")

        return self._render_prompt(
            neighborhood_context=item.get(ContextOrchestrator.column_name("neighborhood"), ""),
            semantic_context=item.get(ContextOrchestrator.column_name("semantic"), ""),
            similar_code_context=item.get(ContextOrchestrator.column_name("similar"), "")
        )

    def _render_random_prompt(self, random_context: str) -> str:
        random_context = (random_context or "").strip()
        if not random_context:
            return ""

        intro_paragraph = (
            "The following shows related code for the given snippet. "
            "We use diff format to highlight co-occurring changes. "
            "You can use this context to understand established development conventions in the "
            "codebase and to assess the impact of the modification in the given snippet."
        )
        return f"## File Change Context\n\n{intro_paragraph}\n\n{random_context}"

    def _render_prompt(
        self,
        neighborhood_context: str,
        semantic_context: str,
        similar_code_context: str,
    ) -> str:
        """Dynamically combine all context fragments into the final prompt."""
        # Define all possible sections with their titles and content
        sections = [
            {
                "title": "Neighborhood for Snippet",
                "intro": "The following shows the given snippet with its surrounding lines.",
                "content": neighborhood_context,
                "is_active": self.config.use_neighborhood_context,
            },
            {
                "title": "Semantically Related Code for Snippet",
                "intro": "The following shows code snippets that are semantically related to the given snippet, identified via language services. Use them to assess the impact of the modification.",
                "content": semantic_context,
                "is_active": self.config.use_semantic_context,
            },
            {
                "title": "Similar Code Patterns for Snippet",
                "intro": "The following code snippets contain lines similar to the modifications in the given snippet. Use them as a reference for better code consistency.",
                "content": similar_code_context,
                "is_active": self.config.use_similar_context,
            }
        ]

        # Filter for sections that are active and have content
        active_sections = [s for s in sections if s["is_active"] and s["content"] and s["content"].strip()]

        if not active_sections:
            return ""

        # Section titles used in the intro paragraph.
        intro_titles = []
        # Full section bodies.
        body_parts = []

        for i, section in enumerate(active_sections):
            # 1. Build the shared section label and title.
            section_letter = chr(ord('A') + i)
            section_header = f"{section_letter}) {section['title']}"

            # 2. Build the title reference for the intro paragraph.
            #    Format: "A) Neighborhood for Snippet"
            intro_titles.append(f'"{section_header}"')

            # 3. Build the full section body.
            section_full_text = (
                f"{section_header}\n\n"
                f"{section['intro']}\n\n"
                f"{section['content'].strip()}"
            )
            body_parts.append(section_full_text)

        # Build the "including ..." clause from the prepared section titles.
        if len(intro_titles) == 1:
            including_clause = intro_titles[0]
        elif len(intro_titles) == 2:
            including_clause = f"{intro_titles[0]} and {intro_titles[1]}"
        else:
            including_clause = f"{', '.join(intro_titles[:-1])}, and {intro_titles[-1]}"

        intro_paragraph = (
            "The following shows related code for the given snippet, including "
            f"{including_clause}. We use diff format to highlight co-occurring changes. "
            "You can use this context to understand established development conventions in the "
            "codebase and to assess the impact of the modification in the given snippet."
        )

        # Combine the intro paragraph with all section bodies.
        final_prompt_parts = [f"## File Change Context\n\n{intro_paragraph}"] + body_parts

        return "\n\n".join(final_prompt_parts)
