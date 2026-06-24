
import os
import sys
import json
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import re

# Add project root to path before importing project modules.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from config import BASE_DIR

from llm_caller import get_model_function
from llm_review_comment_generation.reranking.reranking_prompter import RerankingPrompter

# =========================================================================
#                       Reranking Pipeline Main Execution
# =========================================================================
# Function: Orchestrates the LLM-based reranking process.
#           Phase 1: Run Reranking (Save Raw LLM Input/Output)
#           Phase 2: Format Results (Save detailed candidate CSV with Ranks)
# =========================================================================

# ================= USER CONFIGURATION =================

# 1. Input/Output
MODULE_DIR = os.path.join(BASE_DIR, "llm_review_comment_generation/reranking")
INPUT_DIR = os.path.join(MODULE_DIR, "input")
OUTPUT_DIR = os.path.join(MODULE_DIR, "output")

# Input File Name (Manually or Automatically generated from CodeReviewer)
INPUT_FILENAME = "candidates_from_Sim2Base2(SS)_NotFiltered.csv"
INPUT_PATH = os.path.join(INPUT_DIR, INPUT_FILENAME)

# Reference File for Code Context ($code)
CODE_CONTEXT_FILE = os.path.join(BASE_DIR, "_extended_data/1438_go_from_ref-test.jsonl")

# 2. Model Configuration
LLM_MODEL = "tencent-v3"

# 3. Execution Config
PARALLEL_EXECUTION = True
MAX_WORKERS = 3 * os.cpu_count() if PARALLEL_EXECUTION else 1

# =================================================
def load_code_context(jsonl_path):
    """Loads the code snippets (hunk_change) from reference JSONL."""
    context_map = {}
    if not os.path.exists(jsonl_path):
        print(f"Code context file not found: {jsonl_path}")
        return context_map

    print(f"Loading code context from: {jsonl_path}")
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            try:
                item = json.loads(line)
                proj = item.get('repo') or item.get('proj')
                rid = item.get('id')
                if proj and rid is not None:
                    code = item.get('hunk_change') or item.get('patch') or ""
                    norm_proj = proj.lower().replace('/', '_').replace('-', '_')
                    context_map[(str(norm_proj), str(rid))] = code
            except:
                continue
    return context_map

def load_candidates(csv_path):
    """Loads candidates from CSV and groups by (proj, id)."""
    groups = defaultdict(list)
    if not os.path.exists(csv_path):
        print(f"Input CSV not found: {csv_path}")
        return groups

    print(f"Loading candidates from: {csv_path}")
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            proj = row['proj']
            cid = row['id']
            groups[(proj, cid)].append(row)
    return groups

def process_group_raw(key, candidates, code_context, prompter, response_fn):
    """
    Phase 1: Generates Prompt, Calls LLM, Returns Raw Record.
    Record: {proj, id, GPT_Input, GPT_Output}
    """
    proj, cid = key
    
    # 1. Get Code Context
    code = code_context.get((proj, cid))
    if not code:
        norm_proj = proj.lower().replace('/', '_').replace('-', '_')
        code = code_context.get((norm_proj, cid))
    
    if not code:
        print(f"No code context found for {proj}#{cid}. Skipping.")
        return None

    # 2. Build Prompt
    try:
        prompt = prompter.build_prompt(code_snippet=code, candidates=candidates)
    except Exception as e:
        print(f"Prompt Build Error {proj}#{cid}: {e}")
        return None

    # 3. Call LLM
    try:
        llm_response = response_fn(prompt)
        response_content = llm_response['response']
        
        return {
            "proj": proj,
            "id": cid,
            "GPT_Input": prompt,
            "GPT_Output": response_content
        }
    except Exception as e:
        print(f"LLM Error {proj}#{cid}: {e}")
        return None

def format_output(raw_csv_path, candidates_csv_path, output_formatted_path):
    """
    Phase 2: Reads Raw Output + Original Candidates -> Generates Detailed Formatted CSV.
    Output: proj, id, index, source, source_rank, comment, GPT_Rank
    """
    print(f"Starting Formatting Phase...")
    print(f"Reading Raw LLM Output: {raw_csv_path}")
    print(f"Reading Original Candidates: {candidates_csv_path}")
    
    # 1. Load Raw LLM Outputs
    # Map: (proj, id) -> GPT_Output_String
    raw_map = {}
    if os.path.exists(raw_csv_path):
         with open(raw_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                k = (row['proj'], row['id'])
                raw_map[k] = row['GPT_Output']
    else:
        print("Raw CSV not found!")
        return

    # 2. Load Candidates Helper
    groups = load_candidates(candidates_csv_path)
    
    # 3. Process and Merge
    prompter = RerankingPrompter()
    detailed_rows = []
    
    fieldnames = ["proj", "id", "index", "source", "source_rank", "comment", "GPT_Rank"]
    
    for key, cands in groups.items():
        proj, cid = key
        gpt_output = raw_map.get(key)
        
        ranking = None
        if gpt_output:
            parse_res = prompter.parse_output(gpt_output)
            ranking = parse_res.get('ranking')
        else:
            # Handle missing output
            if len(cands) == 1:
                # Expected behavior for single candidates (Skipped in Phase 1)
                # Ensure it gets Rank 1 eventually (handled by sorting default)
                pass
            else:
                # Unexpected Missing Output for Multi-Candidate
                print(f"⚠️ WARNING: Missing LLM output for Multi-Candidate Case {proj}#{cid} (Candidates: {len(cands)})")
            
            
        # Create Rank Map: "1" -> 1
        rank_map = {}
        if ranking:
            for r_item in ranking:
                m_name = r_item.get('model', '') # "Model 1"
                r_val = r_item.get('rank', -1)
                m = re.search(r'(\d+)', str(m_name))
                if m:
                    idx_str = m.group(1)
                    rank_map[idx_str] = r_val
        
        # Prepare list for sorting: (rank, index_int, index_str)
        rank_data = []
        for cand in cands:
            idx_str = str(cand['index'])
            # Get raw rank, default to huge number if missing
            raw_r = rank_map.get(idx_str, 9999)
            try:
                raw_r = int(raw_r)
            except:
                raw_r = 9999
            
            try:
                idx_int = int(idx_str)
            except:
                idx_int = 99999
            
            rank_data.append({
                'cand': cand,
                'raw_rank': raw_r,
                'model_idx': idx_int
            })
            
        # Sort: Primary=raw_rank (asc), Secondary=model_idx (asc)
        rank_data.sort(key=lambda x: (x['raw_rank'], x['model_idx']))
        
        # Assign new sequential ranks 1..N and map back to index
        index_to_new_rank = {}
        for seq_rank, item in enumerate(rank_data, 1):
            cand = item['cand']
            idx_str = str(cand['index'])
            index_to_new_rank[idx_str] = seq_rank
            
        # Re-sort cands by index for output stability
        # Convert index to int for correct sorting
        cands.sort(key=lambda x: int(x['index']) if str(x['index']).isdigit() else 99999)
        
        for cand in cands:
            idx_str = str(cand['index'])
            new_rank = index_to_new_rank.get(idx_str, "")
            
            row = {
                "proj": cand.get('proj'),
                "id": cand.get('id'),
                "index": cand.get('index'),
                "source": cand.get('source'),
                "source_rank": cand.get('source_rank'),
                "comment": cand.get('comment'),
                "GPT_Rank": new_rank
            }
            detailed_rows.append(row)

    # 4. Save
    print(f"Saving formatted results to: {output_formatted_path}")
    with open(output_formatted_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(detailed_rows)
        
    print("Formatting Done.")


def main():
    # Setup LLM
    print(f"Initializing LLM: {LLM_MODEL}")
    response_fn, llm_params = get_model_function(LLM_MODEL, input_type="prompt")
    prompter = RerankingPrompter()

    # Data
    groups = load_candidates(INPUT_PATH)
    code_context = load_code_context(CODE_CONTEXT_FILE)
    
    all_keys = list(groups.keys())
    total_groups = len(all_keys)
    print(f"Total Cases to Process: {total_groups}")

    # Output Paths
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base_name = os.path.splitext(INPUT_FILENAME)[0]
    
    # Raw Output for Phase 1
    raw_output_filename = f"{base_name}_{LLM_MODEL}(raw).csv"
    raw_output_path = os.path.join(OUTPUT_DIR, raw_output_filename)
    
    # Formatted Output for Phase 2
    formatted_output_filename = f"{base_name}_{LLM_MODEL}(reranked).csv"
    formatted_output_path = os.path.join(OUTPUT_DIR, formatted_output_filename)

    # ---------------------------
    # PHASE 1: EXECUTION
    # ---------------------------
    processed_keys = set()
    raw_fieldnames = ["proj", "id", "GPT_Input", "GPT_Output"]
    
    if os.path.exists(raw_output_path):
        print(f"Resuming Raw Output: {raw_output_path}")
        with open(raw_output_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                processed_keys.add((row['proj'], row['id']))
    else:
        with open(raw_output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=raw_fieldnames)
            writer.writeheader()

            writer.writeheader()
    
    # Identify keys to process (Skip single candidates)
    multi_cand_keys = [k for k in all_keys if len(groups[k]) > 1]
    single_cand_keys = [k for k in all_keys if len(groups[k]) <= 1]
    
    print(f"Single Candidate Cases (Skipped): {len(single_cand_keys)}")
    print(f"Multi Candidate Cases (To Process): {len(multi_cand_keys)}")

    to_process_keys = [k for k in multi_cand_keys if k not in processed_keys]
    print(f"Remaining cases for Phase 1: {len(to_process_keys)}")
    
    if to_process_keys:
        lock = threading.Lock()
        completed_count = 0

        def task(key):
            cands = groups[key]
            rec = process_group_raw(key, cands, code_context, prompter, response_fn)
            return key, rec

        if PARALLEL_EXECUTION:
            print(f"Starting Parallel Phase 1 (Workers={MAX_WORKERS})")
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_map = {executor.submit(task, k): k for k in to_process_keys}
                for future in as_completed(future_map):
                    key = future_map[future]
                    try:
                        _, rec = future.result()
                        if rec:
                            with lock:
                                # Ensure strict CSV writing (handle newlines in prompt/output safely)
                                with open(raw_output_path, 'a', newline='', encoding='utf-8') as f:
                                    writer = csv.DictWriter(f, fieldnames=raw_fieldnames)
                                    writer.writerow(rec)
                    except Exception as e:
                        print(f"Task Failed {key}: {e}")
                    
                    completed_count += 1
                    if completed_count % 10 == 0:
                         print(f"Phase 1 Progress: {completed_count}/{len(to_process_keys)}")
        else:
             print("Starting Sequential Phase 1")
             for k in to_process_keys:
                 key, rec = task(k)
                 if rec:
                     with open(raw_output_path, 'a', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=raw_fieldnames)
                        writer.writerow(rec)
    
    print("Phase 1 Complete.")

    # ---------------------------
    # PHASE 2: FORMATTING
    # ---------------------------
    format_output(raw_output_path, INPUT_PATH, formatted_output_path)
    print(f"Pipeline Complete. Check outputs in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
