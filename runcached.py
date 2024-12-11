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
from pathlib import Path

# Configurable parameters
CACHE_PERIOD_S: float = 27
MAX_WAIT_PREV_S: int = 5
MIN_RAND_S: float = 0
MAX_RAND_S: float = 0


def get_cache_dir() -> Path:
    """
    Return the system's caching directory.
    """
    from tempfile import gettempdir
    return Path(gettempdir())


def generate_command_hash(cmd: list) -> str:
    """
    Generate an MD5 hash for the given command.
    """
    # noinspection InsecureHash
    return hashlib.md5(" ".join(cmd).encode("utf-8")).hexdigest()


def execute_command(cmd: list, data_file: Path, data_file_encoding: str, exit_file: Path, cmd_file: Path) -> None:
    """
    Execute the command and cache its output.
    """
    with data_file.open('w', encoding=data_file_encoding) as f_stdout:
        process = subprocess.Popen(cmd, stdout=f_stdout, stderr=f_stdout)
        process.communicate()
    with exit_file.open('w') as f:
        f.write(str(process.returncode))
    with cmd_file.open('w') as f:
        f.write(" ".join(cmd))


def wait_for_previous_command(pid_file: Path):
    """
    Wait for a previous instance of the command to finish.
    """
    for _ in range(MAX_WAIT_PREV_S):
        if not pid_file.is_file():
            break
        time.sleep(1)
    else:
        logging.error("Timeout waiting for previous command to finish.")
        sys.exit(1)


def main():
    # Setup logging
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        sys.exit(f"Usage: {sys.argv[0]} [-c cache_period_in_s] <command>")

    command: list
    cache_period: float = CACHE_PERIOD_S
    if sys.argv[1] == '-c':
        cache_period = max(0.0, float(sys.argv[2]))
        command = list(sys.argv[3:])
    else:
        command = list(sys.argv[1:])

    command_hash = generate_command_hash(command)
    cache_dir = get_cache_dir()
    pid_file = Path(cache_dir, f"{command_hash}.pid")
    data_file = Path(cache_dir, f"{command_hash}.data")
    exit_file = Path(cache_dir, f"{command_hash}.exit")
    cmd_file = Path(cache_dir, f"{command_hash}.cmd")

    # Random sleep
    if MAX_RAND_S - MIN_RAND_S > 0:
        time.sleep(random.uniform(MIN_RAND_S, MAX_RAND_S))

    # Avoid parallel execution
    wait_for_previous_command(pid_file)

    # Create a PID file
    with pid_file.open('w') as f:
        f.write(str(os.getpid()))

    try:
        # Check cache and execute the command if needed
        if not data_file.is_file() or time.time() - data_file.stat().st_mtime > cache_period:
            # Execute and cache the result
            # Data file encoding must be the same as stdout encoding
            data_file_encoding = sys.stdout.encoding
            if not data_file_encoding:
                data_file_encoding = "utf-8"
            execute_command(command, data_file, data_file_encoding, exit_file, cmd_file)

        try:
            # Output cached data
            # Open in binary mode and copy to stdout buffer directly to avoid unnecessary decoding/encoding
            with data_file.open('rb') as f:
                shutil.copyfileobj(f, sys.stdout.buffer)
            # Flush output here to force SIGPIPE to be triggered while inside this try block.
            sys.stdout.flush()
        except BrokenPipeError:
            # This handles BrokenPipeError on piping the result to 'head' or similar tools.
            # https://docs.python.org/3/library/signal.html#note-on-sigpipe
            # Python flushes standard streams on exit; redirect remaining output to devnull to avoid another
            # BrokenPipeError at shutdown
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            # Python exits with error code 1 on EPIPE
            sys.exit(1)
    finally:
        # Cleanup the PID file
        if pid_file.is_file():
            pid_file.unlink()


if __name__ == "__main__":
    main()
