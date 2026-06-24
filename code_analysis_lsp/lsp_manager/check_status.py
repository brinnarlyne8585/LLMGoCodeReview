# check_status.py (DEALER + Poller mode)
# --------------------------------------------------
# Description:
#   A standalone tool for checking initialization task status
#   from the background lsp_server_manager.
#   It uses DEALER + Poller to communicate with the manager
#   for better robustness.
# --------------------------------------------------
import zmq
import time

from code_analysis_lsp.lsp_config import ZMQ_ENDPOINT


def main():
    context = zmq.Context()

    # Use a DEALER socket instead of REQ.
    socket = context.socket(zmq.DEALER)

    client_id = f"status-checker-{time.time_ns()}".encode('utf-8')
    socket.setsockopt(zmq.IDENTITY, client_id)

    socket.connect(ZMQ_ENDPOINT)

    print(f"Checking initialization status from {ZMQ_ENDPOINT}... (Press Ctrl+C to stop)")

    try:
        while True:
            # Use Poller-based communication.
            try:
                # Send the request.
                socket.send_json({"command": "get_initialization_status"})

                # Create a Poller and wait for the response.
                poller = zmq.Poller()
                poller.register(socket, zmq.POLLIN)

                # Use a short timeout because status checks should return immediately.
                timeout_ms = 5 * 1000
                socks = dict(poller.poll(timeout=timeout_ms))

                if socket in socks:
                    response = socket.recv_json()
                else:
                    # Timeout handling.
                    print(
                        "\nError: Timed out waiting for a response from the manager. It might be busy, frozen, or not running.",
                        flush=True)
                    break

            except zmq.ZMQError as e:
                print(f"\nError: A communication error occurred: {e}", flush=True)
                break

            if "result" in response:
                status = response["result"]
                total = status.get('total_tasks', 0)
                if total == 0 and not status.get('tasks_active', 0) and not status.get('tasks_queued', 0):
                    print("No initialization tasks submitted yet.", flush=True)
                    # If no task exists yet, wait a bit longer before checking again.
                    time.sleep(5)
                    continue

                done = status.get('tasks_done', 0)
                successful = status.get('tasks_successful', 0)
                failed = status.get('tasks_failed', 0)
                active = status.get('tasks_active', 0)
                queued = status.get('tasks_queued', 0)

                progress = (done / total) * 100 if total > 0 else 0

                status_line = (
                    f"Progress: {progress:6.1f}% | "
                    f"Total: {total:4d} | "
                    f"Active: {active:3d} | Queued: {queued:3d} | "
                    f"Done: {done:4d} (Success: {successful}, Failed: {failed})"
                )
                # Pad the line with spaces so the previous line is fully overwritten.
                print(status_line.ljust(80), end='\r', flush=True)

                if done == total and total > 0:
                    print("\nAll initialization tasks complete.", flush=True)
                    break
            else:
                print(f"\nError receiving status: {response.get('error')}", flush=True)
                break

            time.sleep(2)  # Refresh every 2 seconds.

    except KeyboardInterrupt:
        print("\nStopped monitoring.", flush=True)
    finally:
        socket.close()
        context.term()


if __name__ == "__main__":
    main()
