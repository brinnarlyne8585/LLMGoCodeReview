import csv
import os
import threading
import pandas as pd

# ------------------------ File utilities ------------------------ #
# Global lock registry keyed by file path.
file_locks = {}
lock_manager_lock = threading.Lock()

def get_file_lock(file_path):
    """
    Assign a dedicated lock for each file.
    """
    with lock_manager_lock:  # Create locks in a thread-safe way.
        if file_path not in file_locks:
            file_locks[file_path] = threading.Lock()
        return file_locks[file_path]

def synchronized_file_operation(func):
    """
    Decorator that serializes operations per file path.
    """
    def wrapper(file_path, *args, **kwargs):
        file_lock = get_file_lock(file_path)  # Get this file's dedicated lock.
        with file_lock:  # Run the operation under the file lock.
            return func(file_path, *args, **kwargs)
    return wrapper

@synchronized_file_operation
def write_results_to_file(file_path, results):
    """
    Batch-write cached rows to a file under its dedicated lock.
    """
    if not results:
        return
    with open(file_path, mode="a", newline="", encoding="utf-8") as outfile:
        writer = csv.writer(outfile)
        writer.writerows(results)
        # print(f"[INFO] Wrote {len(results)} records to {file_path}.")

@synchronized_file_operation
def update_dataframe(file_path, df, updated_rows):
    """
    Update processed rows in a DataFrame under its dedicated lock.
    """
    for updated_row in updated_rows:
        head_sha, pr_urls = updated_row[3], updated_row[6]
        index = df[(df["Head_SHA"] == head_sha)].index
        if not index.empty:
            df.loc[index, "PR_URLs"] = pr_urls
    df.to_csv(file_path, index=False)
    print(f"[INFO] Updated {len(updated_rows)} rows in DataFrame to {file_path}.")

@synchronized_file_operation
def write_results_to_parquet(file_path, results):
    """Append rows to a Parquet file."""
    if not results:
        return

    df = pd.DataFrame(results)
    # Create a new Parquet file when the target path does not exist.
    if not os.path.exists(file_path):
        df.to_parquet(file_path, index=False, engine="fastparquet")
        print(f"[INFO] Created new Parquet file: {file_path}")
    else:
        # Append rows when the target path already exists.
        df.to_parquet(file_path, index=False, engine="fastparquet", append=True)
        print(f"[INFO] Appended {len(results)} records to {file_path}.")
