import sys
from typing import Optional, Dict, List, Any

from code_context_mangers.code_context_renderer import CodeContextRenderer
from code_context_mangers.run_for_code_context import config_run, tasks_file_path


class BasePromptGenerator:
    def _initialize_managers(self):
        if self.options.get("use_file_content"):
            cfg_in_opt = self.options.get("context_config")
            path_in_opt = self.options.get("context_tasks_path")
            ver_in_opt = self.options.get("context_version")

            cfg_to_use = cfg_in_opt if cfg_in_opt else config_run
            path_to_use = path_in_opt if path_in_opt else tasks_file_path

            renderer_kwargs = self.get_context_renderer_kwargs()
            if ver_in_opt:
                renderer_kwargs["version"] = ver_in_opt

            self.file_manager = CodeContextRenderer(
                config=cfg_to_use,
                tasks_file_path=path_to_use,
                **renderer_kwargs,
            )
        else:
            self.file_manager = None

    def get_context_renderer_kwargs(self) -> Dict[str, Any]:
        return {}

    def show_options(self):
        for k, v in self.options.items():
            print(f"{k}: {v}")

    def get_options(self):
        return self.options.copy()

    def _merge_options(self, options: dict) -> dict:
        merged = self.DEFAULT_OPTIONS.copy()
        merged.update(options or {})
        return merged

    def _render_file_content(
        self,
        comment_url: str,
    ) -> str:
        if not self.options.get("use_file_content"):
            return ""
        return self.file_manager.render_file_content(comment_url)

    def _render_reviewed_patch(
        self,
        code_under_review: str,
        path_under_review: str,
    ) -> str:
        code_under_review = code_under_review.strip()
        path_under_review = path_under_review.strip()
        return f"## Code Snippet for Your Review\n\n{path_under_review}:\n{code_under_review}"

    def _render_comment_command(self) -> str:
        raise NotImplementedError

    def create_prompt(
        self,
        rec: Dict[str, Any],
        specified_cases: Optional[List[str]] = None,
        max_window=sys.maxsize,
    ) -> str:
        proj = rec["proj"]
        msg_id = rec["id"]
        comment_url = rec["comment_url"]
        code_under_review = rec["patch"]
        path_under_review = rec["path"]

        prompt_parts = [
            self.base_instruction,
            self._render_file_content(
                comment_url=comment_url,
            ),
            self._render_reviewed_patch(code_under_review, path_under_review),
            self._render_comment_command(),
        ]
        return "\n\n".join(part for part in prompt_parts if part)
