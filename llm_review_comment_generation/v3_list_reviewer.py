import csv
import json
import os
import sys
from pathlib import Path

from file_utils import write_results_to_file
from llm_review_comment_generation.prompt_builders import review_prompter
from typing import List, Dict, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from llm_caller import get_model_function
from config import BASE_DIR

csv.field_size_limit(sys.maxsize)
max_workers = 3 * os.cpu_count()

def create_composite_key(proj: str, review_id: str) -> str:
    proj = str(proj)
    review_id = str(review_id)
    return f"{proj}||{review_id}"

def review_parallel(input_path: str,
                    output_path: str,
                    parallel: bool = False,
                    use_cache: bool = True,
                    write_output: bool = True):
    global prompter

    """
    Run review generation with optional multithreading.
    """
    log_info = {
        "event": "Called review_parallel()",
        "input": input_path,
        "output_path": output_path,
        "model": llm_model,
        "llm_params": llm_params,
        "prompter_params": prompter_config,
        "use_cache": use_cache,
        "write_output": write_output,
    }
    for k, v in log_info.items():
        print(f'"{k}": "{v}",')

    # 1. Load input records.
    records: List[Dict[str, Any]] = []
    if not os.path.exists(input_path):
        print(f"Input file does not exist: {input_path}")
        return
    with open(input_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if 'hunk_change' in rec:
                    rec['patch'] = rec.pop('hunk_change')
                # Keep compatibility with records that use repo instead of proj.
                if 'proj' not in rec and 'repo' in rec:
                    rec['proj'] = rec['repo']
                if all(k in rec for k in ['patch', 'proj', 'id', 'msg']):
                    records.append(rec)
            except json.JSONDecodeError:
                continue

    # 2. Build processed composite keys for cache lookup.
    processed_keys = set()
    if use_cache and os.path.exists(output_path):
        print(f"[INFO] Using CACHE from: {output_path}")
        with open(output_path, 'r', encoding='utf-8', newline='') as f_out:
            reader = csv.DictReader(f_out)
            for row in reader:
                if all(key in row for key in ['proj', 'id', 'msg']):
                    processed_keys.add(create_composite_key(row['proj'], row['id']))
        print(f"[INFO] Found {len(processed_keys)} already processed items.")
    else:
        print("[INFO] No CACHE used (or file missing). Starting fresh.")

    # 3. Keep only records that still need processing.
    to_process = [rec for rec in records
                  if create_composite_key(rec['proj'], rec['id']) not in processed_keys]

    # 4. Prepare output file.
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    fieldnames = ['proj', 'id', 'msg', 'patch', 'GPT_Input', 'GPT_Output']
    file_exists = os.path.exists(output_path)

    if write_output and not file_exists:
        with open(output_path, 'w', encoding='utf-8', newline='') as f_out:
            writer = csv.writer(f_out)
            writer.writerow(fieldnames)
    elif not write_output:
        print("[WARN] write_output=False. Results will NOT be saved to disk!")

    total_processed = 0
    def process_one(rec: Dict[str, Any],) -> Optional[Dict[str, Any]]:

        print(f"[INFO] Processed {total_processed}: {rec['proj']}#{rec['id']}")

        proj = rec['proj']
        review_id = rec['id']
        msg = rec['msg']
        patch = rec['patch']

        # --- 1. Build prompt. ---
        prompt = prompter.create_prompt(rec=rec)

        # --- 2. Call LLM. ---
        llm_response = {}
        if DRY_RUN_WITHOUT_LLM:
            response_content = "[DRY_RUN_WITHOUT_LLM]"
        else:
            try:
                llm_response = response_fn(prompt)
                response_content = llm_response['response']

            except Exception as e:
                err_msg = str(e)
                if "Input exceeds maximum character limit" in err_msg or "input length too long" in err_msg:
                    response_content = "[INPUT_TOO_LONG]"
                    print(f"[INPUT_TOO_LONG] {rec['proj']}#{rec['id']}: {err_msg}")

        return {
            'proj': proj,
            'id': review_id,
            'msg': msg,
            'patch': patch,
            'GPT_Input': prompt,
            'GPT_Output': response_content,
        }

    # Run in parallel or sequentially.
    if parallel:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for res in executor.map(process_one, to_process):
                if res:
                    if write_output:
                        row = [res[col] for col in fieldnames]
                        write_results_to_file(output_path, [row])
                    total_processed += 1
                    print(f"[INFO] Processed {total_processed}: {res['proj']}#{res['id']}")
    else:
        # Open the output file only when write_output is enabled.
        f_out_ctx = open(output_path, 'a', encoding='utf-8', newline='') if write_output else None
        
        try:
            writer = csv.DictWriter(f_out_ctx, fieldnames=fieldnames) if f_out_ctx else None
            
            for rec in to_process:
                res = process_one(rec)
                if res and res.get('GPT_Output'): # Ensure valid result
                    if writer:
                        writer.writerow(res)
                        # Flush immediately if needed (optional)
                        f_out_ctx.flush()
                    total_processed += 1
                    print(f"[INFO] Processed {total_processed}: {res['proj']}#{res['id']}")
        finally:
            if f_out_ctx:
                f_out_ctx.close()

    print(f"Completed processing {total_processed} new records.")
    if write_output:
        print(f"Saved to {output_path}")

def format_output_strip(src, dst):
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    with open(src, 'r', encoding='utf-8', newline='') as f_in, \
            open(dst, 'w', encoding='utf-8', newline='') as f_out:
        reader = csv.DictReader(f_in)
        fieldnames = ['proj', 'id', 'msg', 'GPT_Output_Formatted']
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()

        count = 0
        for row in reader:
            raw = row.get('GPT_Output', '')
            formatted_text = raw.strip()
            writer.writerow({
                'proj': row.get('proj'),
                'id': row.get('id'),
                'msg': row.get('msg'),
                'GPT_Output_Formatted': formatted_text
            })
            count += 1
        print(f"Strip Format: Processed {count} rows -> {dst}")


from code_context_mangers.code_context_config import ContextConfig
_MODE2CFG = {
    "neighborhood": ContextConfig(use_semantic_context=False, use_similar_context=False),
    "semantic": ContextConfig(
        use_neighborhood_context=False,
        use_semantic_context=True,
        use_similar_context=False,
    ),
    "similar": ContextConfig(
        use_neighborhood_context=False,
        use_semantic_context=False,
        use_similar_context=True,
    ),
    "semantic_similar": ContextConfig(
        use_neighborhood_context=False,
        use_semantic_context=True,
        use_similar_context=True,
    ),
    "neighborhood_semantic": ContextConfig(
        use_semantic_context=True,
        use_similar_context=False
    ),
    "neighborhood_similar": ContextConfig(
        use_semantic_context=False,
        use_similar_context=True
    ),
    "neighborhood_semantic_similar": ContextConfig(
        use_semantic_context=True,
        use_similar_context=True
    ),
    "random": ContextConfig(
        use_neighborhood_context=False,
        use_semantic_context=False,
        use_similar_context=False,
        use_random_context=True,
    ),
}

def resolve_prompt_version(prompt_version: str):
    if prompt_version.startswith("list_p"):
        prompt_number = prompt_version.removeprefix("list_p")
        return "review_list_prompter", f"list_prompt_{prompt_number}"
    if prompt_version.startswith("single_p"):
        return "review_prompter", prompt_version
    raise ValueError(f"Unknown prompt version: {prompt_version}")

def pipeline():

    import llm_review_comment_generation.prompt_builders.review_list_prompter as review_list_prompter

    def run_reviewer(ctx_mode: str,
                     tag: str,
                     run_id: int,
                     use_file_content_flag: bool = True):
        """
        Run one reviewer configuration.
        """

        # 1. Resolve context config from mode.
        cfg = _MODE2CFG.get(ctx_mode)
        if not cfg:
            raise ValueError(f"Unknown ctx_mode: {ctx_mode}")

        # Pass the resolved config to PromptGenerator.
        global prompter, prompter_config
        
        # Select the prompter implementation.
        if PROMPTER_TYPE == "review_prompter":
            PrompterClass = review_prompter.PromptGenerator
        elif PROMPTER_TYPE == "review_list_prompter":
            PrompterClass = review_list_prompter.PromptGenerator
        else:
            raise ValueError(f"Unknown PROMPTER_TYPE: {PROMPTER_TYPE}")
            
        print(f"[INFO] Using Prompter: {PrompterClass.__module__}.{PrompterClass.__name__}")

        prompter = PrompterClass(
            input_comment_file=input_file,
            # --- Core context configuration. ---
            use_file_content=use_file_content_flag,
            context_config=cfg,
            context_version=tag,
        )
        prompter_config = prompter.get_options()

        # 2. Build output file path.
        version = OUTPUT_VERSION
        p = Path(input_file)
        name_without_ext = p.stem
        out_dir = f"{BASE_DIR}/llm_review_comment_generation/output(publish)/{name_without_ext}"
        mode_for_name = ctx_mode
        ctx_version = tag
        if use_file_content_flag:
            out_file = (
                f"{out_dir}/"
                f"{llm_model}"
                f"({version})"
                f"({mode_for_name}({ctx_version}))"
                f"({run_id}).csv"
            )
        else:
            out_file = (
                f"{out_dir}/"
                f"{llm_model}"
                f"({version})"
                f"({run_id}).csv"
            )

        # Run review generation.
        review_parallel(
            parallel=parallel,
            input_path=input_file,
            output_path=out_file,
            use_cache=USE_CACHE,
            write_output=WRITE_OUTPUT,
        )

        # Only format/evaluate if writing output is enabled
        if WRITE_OUTPUT:
            formatted = out_file.replace('.csv', '(f).csv')
            format_output_strip(out_file, formatted)

    for mode, context_version, use_file_flag in EXPERIMENTS:
        tag = context_version
        for run_id in RUN_IDS:
            run_reviewer(mode, tag, run_id, use_file_flag)

    print("\nAll done.")



if __name__ == "__main__":
    import llm_review_comment_generation.prompt_builders.review_list_prompter as review_list_prompter

    USE_CACHE = True       # Skip existing (proj, id) outputs.
    WRITE_OUTPUT = True    # Write CSV output. Set False for dry runs.
    DRY_RUN_WITHOUT_LLM = True  # Generate GPT_Input without sending model requests.
    parallel = True        # Run in parallel when True.

    # Main settings for one experiment.
    llm_model = "tencent-v3"  # Model name.
    PROMPT_VERSION = "list_p1"  # Prompt version.
    # Context configuration.
    EXPERIMENTS = [
        ("neighborhood_similar", "26A-similar-medium-0.001", True),
    ]
    RUN_IDS = [0]  # Run ids to execute.

    OUTPUT_VERSION = PROMPT_VERSION
    PROMPTER_TYPE, PROMPT_NAME = resolve_prompt_version(PROMPT_VERSION)
    if PROMPTER_TYPE == "review_prompter":
        review_prompter.review_task_instruction = getattr(review_prompter, PROMPT_NAME)
    elif PROMPTER_TYPE == "review_list_prompter":
        review_list_prompter.review_task_instruction = getattr(review_list_prompter, PROMPT_NAME)
    response_fn, llm_params = get_model_function(llm_model, input_type="prompt")

    input_file = f"{BASE_DIR}/_extended_data/1438_go_from_ref-test.jsonl"

    pipeline()
