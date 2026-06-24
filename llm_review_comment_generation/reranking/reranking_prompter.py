import json
import re
import ast
from string import Template
from typing import List, Dict, Any, Optional

# =========================================================================
#                       Reranking Prompter & Parser
# =========================================================================
# Function: Manages the LLM prompt construction and output parsing for the
#           reranking task.
# =========================================================================


# PROMPT TEMPLATE (Copied from select_best_by_llm.py)
EVALUATION_PROMPT_TEMPLATE = Template(
"""As a thorough and unbiased AI evaluator, you're tasked to rank several AI models based on the quality of their code review comments to a given problematic Go code snippet. For each model, you will assign a score from 1-10 (higher scores indicate better performance) for each of the following ten metrics. Upon scoring, you will create an overall leaderboard for the models.

Please note: 

- Avoid any positional bias. The order of comments must not influence your ranking.
- The length of the comments should not affect your evaluation.
- Models' names should not influence your judgment.
- Maintain objectivity throughout the process.

The ten metrics are:

1. **Readability**: How clear and easily understood is the comment?
2. **Relevance**: Does the comment directly address the code's issues, and avoids unrelated information?
3. **Explanation Clarity**: Does the comment explain the issues effectively, going beyond merely identifying the problem?
4. **Problem Identification**: Does the comment accurately and clearly describe the code's bugs?
5. **Actionability**: Does the comment provide practical, actionable advice for rectifying the code errors?
6. **Completeness**: Does the comment provide a comprehensive review of all issues in the problematic code?
7. **Specificity**: How precisely does the comment pinpoint the specific issues within the code?
8. **Contextual Adequacy**: Does the comment directly relate to the context of the problematic code?
9. **Consistency**: How uniform is the comment's quality, relevance, and other aspects compared to the model's previous samples? If there are no previous samples available, assign a score of 10.
10. **Brevity**: How succinct and to-the-point is the comment, conveying necessary information in as few words as possible?

Problematic code snippet:
$code

Comments from the models:
$comment_list

After scoring, rank the models by their overall performance quality. A rank of 1 signifies the best output. If models tie, assign them the same rank corresponding to their position. The subsequent rank is skipped for the next model. 

For example, if two models tie for first place, both receive a rank of 1, and the next model gets a rank of 3. If three models tie for second place, all are ranked 2, and the next model, if any, is ranked 5. 

Structure your output as follows:

### Scoring:
[
    {'model': <model-name>, 'score': [list of scores in order]},
    {'model': <model-name>, 'score': [list of scores in order]},
    ...
]

### Chain-of-Thoughts:
{Provide a short explanation for your ranking}

### Ranking:
[
    {'model': <model-name>, 'rank': <model-rank>},
    {'model': <model-name>, 'rank': <model-rank>},
    ...
]

The sections "Scoring" and "Ranking" must be valid Python dictionary lists, ready to be directly executed in Python. Each section should begin with its respective title, exclusively: "### Scoring:", "### Chain-of-Thoughts:", and "### Ranking:". The goal is to produce a ranking most human evaluators would agree with.""")


class RerankingPrompter:
    def __init__(self):
        pass

    def build_prompt(self, code_snippet: str, candidates: List[Dict[str, Any]]) -> str:
        """
        Constructs the evaluation prompt.
        :param code_snippet: The code snippet being reviewed ($code).
        :param candidates: List of candidate dicts, must have 'index' and 'comment'.
        :return: Formatted prompt string.
        """
        # Format comment list
        # "Model {index}:\n\"\"\"{comment}\"\"\"\n"
        code_snippet = code_snippet.strip()
        comment_list_parts = []
        for cand in candidates:
            # Index is typically 1-based, ensure it's used as the "Model ID"
            idx = cand.get('index')
            comment = cand.get('comment', '').strip()
            part = f"Model {idx}:\n\"\"\"{comment}\"\"\"\n"
            comment_list_parts.append(part)
        
        comment_list_str = "\n".join(comment_list_parts)
        
        # Substitute into template
        try:
            full_prompt = EVALUATION_PROMPT_TEMPLATE.substitute(
                code=code_snippet,
                comment_list=comment_list_str
            )
            return full_prompt
        except KeyError as e:
            raise RuntimeError(f"Missing template variable: {e}")

    def parse_output(self, llm_output: str) -> Dict[str, Any]:
        """
        Parses the LLM output to extract the Ranking list.
        Returns a dict: {'ranking': [{'model': 'Model 1', 'rank': 1}, ...]}
        """
        parsed_data = {'ranking': None}
        try:
            # Regex to find the Ranking section (List of Dicts)
            # Looks for: ### Ranking:\s*([...])
            ranking_match = re.search(r"### Ranking:\s*(\[.*?\])", llm_output, re.DOTALL)
            
            if ranking_match:
                ranking_str = ranking_match.group(1)
                
                try:
                    # Use ast.literal_eval for safe evaluation of Python literals
                    # This handles single quotes, None, True/False naturally
                    parsed_data['ranking'] = ast.literal_eval(ranking_str)
                except (ValueError, SyntaxError):
                    # Fallback or Log
                    pass
                    
        except Exception as e:
            # Just return empty/None ranking, let caller handle
            pass

        return parsed_data
