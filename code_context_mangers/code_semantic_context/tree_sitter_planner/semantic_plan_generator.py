
import json
import re
import csv
import os
import argparse
from typing import List, Dict
from tqdm import tqdm

from code_context_mangers.code_semantic_context.tree_sitter_planner.planner_git_utils import TreeSitterUtils
from code_context_mangers.code_semantic_context.tree_sitter_planner.ast_change_analyzer import TreeSitterAnalyzer
from code_context_mangers.code_semantic_context.tree_sitter_planner.review_rule_matcher import RuleEngine
from code_analysis_lsp.lsp_config import REPOS_BASE_DIR
from config import BASE_DIR

# ================= Configuration =================
MODULE_DIR = f"{BASE_DIR}/code_context_mangers/code_semantic_context/tree_sitter_planner"
INPUT_FILE = f"{BASE_DIR}/_extended_data/1438_go_from_ref-test.jsonl"
OUTPUT_DIR = f"{MODULE_DIR}/output"

# Identifier mode:
#   None         - disable identifier-level rules and use the existing 14 rules
#   "definition" - run get_definition for all identifiers (Rule-15)
#   "reference"  - run get_references for all identifiers (Rule-16)
#   "both"       - run get_definition + get_references for all identifiers (Rule-17)
IDENTIFIER_MODE = "both"
# =========================================

class TreeSitterPlanner:
    def __init__(self, repos_base_dir: str, identifier_mode: str = None):
        """
        Args:
            repos_base_dir: repository root directory
            identifier_mode: controls identifier-level rule mode
                - None: disabled by default
                - "definition": run get_definition only (Rule-15)
                - "reference": run get_references only (Rule-16)
                - "both": run get_definition + get_references (Rule-17)
        """
        self.repos_base_dir = repos_base_dir
        self.analyzer = TreeSitterAnalyzer()
        self.rule_engine = RuleEngine(identifier_mode=identifier_mode)

    def process_single_task(self, task: Dict) -> Dict:
        """
        Process one task item.
        Return None if it cannot be processed or an error occurs.
        """
        comment_url = task.get('comment_url', '')

        project_dir_name = ""
        match = re.search(r'repos/([^/]+)/([^/]+)', comment_url)
        if match:
            owner, repo = match.groups()
            project_dir_name = f"{owner}_{repo}"
        
        if not project_dir_name:
            print(f"Skipping task index {task.get('index', '?')}: Could not extract repo from URL or field. URL: {comment_url}")
            return None

        repo_root = os.path.join(self.repos_base_dir, project_dir_name)
        file_path = task.get('path', '')
        new_sha = task.get('original_commit_id', '')
        old_sha = task.get('merge_base_sha', '')
        hunk = task.get('hunk_change', '')
        
        if not (file_path and new_sha and old_sha and hunk):
            # Missing required fields.
            print(f"Skipping {comment_url}: Missing essential fields (path, shas, or hunk)")
            return None
        
        if not os.path.exists(repo_root):
            # Local repository does not exist.
            print(f"Skipping {comment_url}: Repo not found at {repo_root}")
            return None
            
        # 1. Get file content.
        old_content = TreeSitterUtils.get_file_content_at_commit(repo_root, old_sha, file_path)
        new_content = TreeSitterUtils.get_file_content_at_commit(repo_root, new_sha, file_path)
        
        if old_content is None: old_content = ""
        if new_content is None: new_content = ""
        
        # 2. Parse hunk.
        old_lines, new_lines = TreeSitterUtils.parse_hunk_lines(hunk)
        
        # 3. Analyze.
        changes, new_tree, old_tree, node_mapping = self.analyzer.analyze_changes(old_content, new_content, old_lines, new_lines)
        
        # 4. Filter / rule engine.
        commands = self.rule_engine.evaluate(changes, new_tree, new_lines, old_tree, node_mapping)
        
        # 5. Format output.
        # Add indices required by the output format.
        for i, cmd in enumerate(commands):
            cmd['index'] = i + 1
        
        parsed_plan = {"commands": commands}
        
        return {
            "comment_url": comment_url,
            "parsed_plan_list_json": json.dumps(parsed_plan, ensure_ascii=False)
        }

    def process_file(self, input_jsonl: str, output_csv: str):
        print(f"Processing {input_jsonl} -> {output_csv}")
        print(f"Base Repo Dir: {self.repos_base_dir}")
        
        results = []
        
        with open(input_jsonl, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        for line in tqdm(lines):
            try:
                task = json.loads(line)
            except:
                continue
            
            result = self.process_single_task(task)
            if result:
                results.append(result)
            
        # Write CSV.
        os.makedirs(os.path.dirname(output_csv), exist_ok=True)
        keys = ["comment_url", "parsed_plan_list_json"]
        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)

if __name__ == "__main__":
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    input_filename = os.path.basename(INPUT_FILE)
    output_filename = input_filename.replace('.jsonl', '_plan(both).csv')
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    # Read repository root and identifier_mode from configuration.
    planner = TreeSitterPlanner(REPOS_BASE_DIR, identifier_mode=IDENTIFIER_MODE)
    planner.process_file(INPUT_FILE, output_path)
