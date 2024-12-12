#! /usr/bin/env python3
# -*- Mode: Python; py-indent-offset: 4; coding: utf-8 -*-
# vim: fenc=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab

# runcached
# Execute commands while caching their output for subsequent calls. 
# Command output will be cached for $cacheperiod and replayed for subsequent calls
#
# 2012
# Author Spiros Ioannou sivann <at> gmail.com
#
# 2024
# Author Pavel Vitis <pavelvitis@gmail.com>

from __future__ import annotations

__authors__ = [
    "Spiros Ioannou sivann <at> gmail.com",
    "Pavel Vitiš <pavelvitis@gmail.com>",
]
__maintainer__ = "Pavel Vitis <pavelvitis@gmail.com"
__license__ = "Apache License, Version 2.0"
__status__ = "Development"

import hashlib
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Iterable, Optional

import psutil

# Configurable parameters
CACHE_PERIOD_S: float = 20
MAX_WAIT_PREV_S: int = 5
MIN_RAND_S: float = 0
MAX_RAND_S: float = 0


def get_cache_dir() -> Path:
    """
    Return the system's caching directory.
    """
    from tempfile import gettempdir
    return Path(gettempdir())


def generate_command_hash(command: Iterable[str]) -> str:
    """
    Generate an MD5 hash for the given command.
    """
    # noinspection InsecureHash
    return hashlib.md5(" ".join(command).encode("utf-8")).hexdigest()


def execute_command(command: Sequence[str], cmd_file: Path, exit_file: Path, output_cache_file: Path,
                    output_cache_file_encoding: str = "utf-8") -> int:
    """
    Execute the command and cache its output.
    """
    with output_cache_file.open('w', encoding=output_cache_file_encoding) as f_stdout:
        process = subprocess.Popen(command, stdout=f_stdout, stderr=f_stdout)
        process.communicate()

    with exit_file.open('w') as f:
        f.write(str(process.returncode))
    with cmd_file.open('w') as f:
        f.write(" ".join(command))

    return process.returncode


def execute_command_and_cache_output(command: Iterable[str], cache_period: float) -> (Path, int):
    cache_dir = get_cache_dir()
    command_hash = generate_command_hash(command)
    output_cache_file = Path(cache_dir, f"{command_hash}.data")
    exit_file = Path(cache_dir, f"{command_hash}.exit")
    cmd_file = Path(cache_dir, f"{command_hash}.cmd")

    if output_cache_file.is_file() and time.time() - output_cache_file.stat().st_mtime <= cache_period:
        return output_cache_file, int(exit_file.read_text())

    # Execute and cache the result
    # Output cache file encoding must be the same as stdout encoding
    cache_file_encoding = sys.stdout.encoding
    if not cache_file_encoding:
        cache_file_encoding = "utf-8"
    return_code = execute_command(command, cmd_file, exit_file, output_cache_file,
                                  output_cache_file_encoding=cache_file_encoding)

    return output_cache_file, return_code


def wait_for_previous_command(pid_file: Path) -> bool:
    """
    Wait for a previous instance of the command to finish.
    """
    for _ in range(MAX_WAIT_PREV_S):
        if not pid_file.is_file():
            # PID file cleaned up, process finished
            return True
        time.sleep(1)
    else:
        # Timeout waiting for previous command to finish.
        # Let's figure out if it's just stale lock and clean it.
        try:
            pid: int = int(pid_file.read_text())
        except (ValueError, OSError):
            # Corrupted or not readable PID file
            pass
        else:
            if not psutil.pid_exists(pid):
                # Stale lock file, remove it
                pid_file.unlink()
                return True

        logging.error("Timeout waiting for previous command to finish.")
        return False


def send_output_to_stdout(output_cache_file: Path) -> bool:
    try:
        # Output cached data
        # Open in binary mode and copy to stdout buffer directly to avoid unnecessary decoding/encoding
        with output_cache_file.open('rb') as f:
            shutil.copyfileobj(f, sys.stdout.buffer)
        # Flush output here to force SIGPIPE to be triggered while inside this try block.
        sys.stdout.flush()
        return True
    except BrokenPipeError:
        # This handles BrokenPipeError on piping the result to 'head' or similar tools.
        # https://docs.python.org/3/library/signal.html#note-on-sigpipe
        # Python flushes standard streams on exit; redirect remaining output to devnull to avoid another
        # BrokenPipeError at shutdown
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        # Python exits with error code 1 on EPIPE
        return False


def create_pid_file(command: Iterable[str]) -> Optional[Path]:
    cache_dir = get_cache_dir()
    command_hash = generate_command_hash(command)
    pid_file = Path(cache_dir, f"{command_hash}.pid")

    # Random sleep
    if MAX_RAND_S - MIN_RAND_S > 0:
        time.sleep(random.uniform(MIN_RAND_S, MAX_RAND_S))

    # Avoid parallel execution
    if not wait_for_previous_command(pid_file):
        return None

    # Create a PID file
    with pid_file.open('w') as f:
        f.write(str(os.getpid()))

    return pid_file


def print_usage() -> None:
    print(f"Usage: {os.path.basename(sys.argv[0])} [-c cache_period_in_s (float)] <command>")
    print(f"  Default cache period is {CACHE_PERIOD_S:.2f}s")


def main():
    # Setup logging
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command: list
    cache_period: float = CACHE_PERIOD_S
    if sys.argv[1] == '-c':
        cache_period = max(0.0, float(sys.argv[2]))
        command = list(sys.argv[3:])
    else:
        command = list(sys.argv[1:])

    # Handle end of parameters mark
    if command[0] == "--":
        command.pop(0)

    if not command:
        print_usage()
        sys.exit(1)

    pid_file: Path = create_pid_file(command)
    # Timeout waiting for already running process
    if pid_file is None:
        sys.exit(2)

    try:
        # Check cache and execute the command if needed
        cached_output: Path
        return_code: int
        cached_output, return_code = execute_command_and_cache_output(command, cache_period)

        stdout_ok: bool = send_output_to_stdout(cached_output)

        if stdout_ok:
            sys.exit(return_code)
        else:
            # On pipe-broken error
            sys.exit(1)
    finally:
        # Cleanup the PID file
        if pid_file.is_file():
            pid_file.unlink()


if __name__ == "__main__":
    main()
