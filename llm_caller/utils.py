import time
import functools
import concurrent.futures


def timeout(seconds):
    """Decorator to timeout a function after specified seconds"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(func, *args, **kwargs)
                try:
                    return future.result(timeout=seconds)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f'The function "{func.__name__}" timed out after {seconds} seconds'
                    )

        return wrapper

    return decorator


def is_exceed_character_limit(error_message):
    """Check if error is related to character limit exceeded"""
    character_limit_error_arr = [
        "code: 336007, msg: the max input characters is 20000",  # Llama error
        "context_length_exceeded",
        "string_above_max_length",
        "This model's maximum context length is 65536 tokens.", # Deepseek
        "input length too long",
    ]
    return any(error in error_message for error in character_limit_error_arr)


def get_completion_with_retry(get_completion_func, input_data, max_retries=3, retry_delay=30):
    """Retry function for API calls with error handling"""
    retries = 0
    while True:
        try:
            response = get_completion_func(input_data)
            if response is not None:
                return response
        except TimeoutError as e:
            print(f"Timeout exception: {e}")
            if retries >= max_retries:
                raise
        except Exception as e:
            error_message = str(e)
            if is_exceed_character_limit(error_message):
                raise ValueError("Input exceeds maximum character limit.") from e
            elif 'inappropriate content.' in error_message:
                print("Inappropriate content detected in the following input:")
                print(input_data)
                return {'response':""}
            print(f"An exception occurred: {e}, Retrying in {retry_delay} seconds...")
            if retries >= max_retries:
                raise
            time.sleep(retry_delay)

        retries += 1
