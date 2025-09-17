#!/usr/bin/env python3
import asyncio
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import tty
import types

# --- Configuration ---
# The minimum time, in seconds, between consecutive backspaces.
BACKSPACE_DELAY = 0.1
# ---------------------


def _get_terminal_size() -> tuple[int, int]:
    """Gets the size of the current terminal as a (rows, cols) tuple."""
    s = struct.pack("hh", 0, 0)
    try:
        return struct.unpack(
            "hh", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
        )
    except OSError:
        return 24, 80  # A reasonable default


async def handle_stdin(queue: asyncio.Queue[bytes | None], master_fd: int) -> None:
    """Coroutine to read from stdin, throttle backspaces, and write to the child."""
    while True:
        user_input = await queue.get()
        if user_input is None:
            break

        for char_code in user_input:
            char = bytes([char_code])

            os.write(master_fd, char)
            # You need to wait _after_ a backspace - backspaces seem to break handling of the _next_ character.
            if char in (b"\x08", b"\x7f"):
                await asyncio.sleep(BACKSPACE_DELAY)
        queue.task_done()


async def handle_child_output(queue: asyncio.Queue[bytes | None]) -> None:
    """Coroutine to read from the child's output queue and write to stdout."""
    while True:
        child_output = await queue.get()
        if child_output is None:
            break

        sys.stdout.buffer.write(child_output)
        sys.stdout.flush()
        queue.task_done()


async def async_main(command: list[str]) -> None:
    """The main asynchronous part of the script."""
    # Fork the process. The child gets a new pseudo-terminal.
    pid, master_fd = pty.fork()

    if pid == pty.CHILD:
        # We are the child process. Execute the user's command.
        try:
            os.execvp(command[0], command)
        except FileNotFoundError:
            print(f"Error: Command not found: {command[0]}")
            sys.exit(1)

    # --- Parent Process Logic ---
    loop = asyncio.get_running_loop()
    stdin_queue = asyncio.Queue()
    child_output_queue = asyncio.Queue()

    # Set the initial size of the child's pty to match our own.
    rows, cols = _get_terminal_size()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("hh", rows, cols))

    # Store original tty settings to restore them on exit
    original_tty_attrs = termios.tcgetattr(sys.stdin)

    # Set the user's terminal to raw mode
    tty.setraw(sys.stdin.fileno())

    def on_stdin_ready() -> None:
        """Callback for when stdin has data to read."""
        try:
            data = os.read(sys.stdin.fileno(), 1024)
            if data:
                stdin_queue.put_nowait(data)
            else:
                stdin_queue.put_nowait(None)  # Signal EOF
        except OSError:
            stdin_queue.put_nowait(None)

    def on_child_ready() -> None:
        """Callback for when the child process has data to read."""
        try:
            data = os.read(master_fd, 1024)
            if data:
                child_output_queue.put_nowait(data)
            else:
                child_output_queue.put_nowait(None)  # Signal EOF
        except OSError:
            child_output_queue.put_nowait(None)

    # Add readers to the event loop. These will call the callbacks when data is ready.
    loop.add_reader(sys.stdin.fileno(), on_stdin_ready)
    loop.add_reader(master_fd, on_child_ready)

    # Create the main concurrent tasks
    stdin_handler_task = asyncio.create_task(handle_stdin(stdin_queue, master_fd))
    child_output_handler_task = asyncio.create_task(
        handle_child_output(child_output_queue)
    )

    # --- Cleanup and Signal Handling ---
    def cleanup(signum: int | None = None) -> None:
        """Restore terminal and clean up tasks."""
        loop.remove_reader(sys.stdin.fileno())
        loop.remove_reader(master_fd)
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_tty_attrs)
        stdin_handler_task.cancel()
        child_output_handler_task.cancel()
        print("\r")

    # Handle window resizing
    def on_resize(signum: int, frame: types.FrameType | None) -> None:
        rows, cols = _get_terminal_size()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("hh", rows, cols))

    signal.signal(signal.SIGWINCH, on_resize)

    # Wait for either task to complete (which happens on EOF)
    await asyncio.wait(
        [stdin_handler_task, child_output_handler_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    cleanup()


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command> [args...]")
        sys.exit(1)

    try:
        asyncio.run(async_main(sys.argv[1:]))
    except OSError:
        # This can happen if the child process exits unexpectedly
        pass
    except KeyboardInterrupt:
        print("\rExiting on KeyboardInterrupt.")


if __name__ == "__main__":
    main()
