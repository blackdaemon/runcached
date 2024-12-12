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

import argparse
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from hashlib import md5
from pathlib import Path
from typing import Iterable, Optional

import psutil

# Configurable parameters
DEFAULT_CACHE_TIMEOUT_S: float = 20
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
    return md5(" ".join(command).encode("utf-8")).hexdigest()


def execute_command(command: Sequence[str], exit_file: Path, output_cache_file: Path,
                    output_cache_file_encoding: str = "utf-8") -> int:
    """
    Execute a command and redirect results into cache files:

        - execution return_code into exit_file
        - stdout & stderr into output_cache_file
    """
    with output_cache_file.open('w', encoding=output_cache_file_encoding) as f_stdout:
        process = subprocess.Popen(command, stdout=f_stdout, stderr=f_stdout)
        process.communicate()

    with exit_file.open('w') as f:
        f.write(str(process.returncode))

    return process.returncode


def execute_or_get_cached_result(command: Iterable[str], cache_period_sec: float) -> (Path, int):
    cache_dir = get_cache_dir()
    command_hash = generate_command_hash(command)
    output_cache_file = Path(cache_dir, f"{command_hash}.data")
    exit_file = Path(cache_dir, f"{command_hash}.exit")
    cmd_file = Path(cache_dir, f"{command_hash}.cmd")

    if output_cache_file.is_file() and time.time() - output_cache_file.stat().st_mtime <= cache_period_sec:
        return output_cache_file, int(exit_file.read_text())

    if not cmd_file.is_file():
        with cmd_file.open('w') as f:
            f.write(" ".join(command))

    # Execute and cache the result
    # Output cache file encoding must be the same as stdout encoding
    cache_file_encoding = sys.stdout.encoding
    if not cache_file_encoding:
        cache_file_encoding = "utf-8"
    return_code = execute_command(command, exit_file, output_cache_file,
                                  output_cache_file_encoding=cache_file_encoding)

    return output_cache_file, return_code


def wait_for_previous_command(pid_file: Path, wait_time_sec: int) -> bool:
    """
    Wait for a previous instance of the command to finish by checking for the existence
    of the given pid_file.

    Returns True if the pid_file disappears within a time limit, False on timeout.

    Also handles stale pid_file by checking if the process is still running. If it's not running,
    the pid_file is removed and function returns True.

    :returns: Return True on success, False on timeout
    """
    for _ in range(wait_time_sec):
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


def send_text_to_stdout(text_file: Path) -> bool:
    """
    Send text file to stdout.

    Broken pipe error is handled by sending the rest of the text to /dev/null.

    See for more details:
    https://docs.python.org/3/library/signal.html#note-on-sigpipe

    :param text_file:
    :return: Return True on complete output, False on BrokenPipeError
    """
    try:
        # Output cached data
        # Open in binary mode and copy to stdout buffer directly to avoid unnecessary decoding/encoding
        with text_file.open('rb') as f:
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
    """
    Create PID file for given command.

    If there is a process with the same command already runhing, wait for its completion.

    Return None on timeout waiting for already running process.

    :param command:
    :return: PID if PID file has been successfully created or None if there was a timeout waiting for already running process
    """
    cache_dir = get_cache_dir()
    command_hash = generate_command_hash(command)
    pid_file = Path(cache_dir, f"{command_hash}.pid")

    # Random sleep
    if MAX_RAND_S - MIN_RAND_S > 0:
        time.sleep(random.uniform(MIN_RAND_S, MAX_RAND_S))

    # Avoid parallel execution
    if not wait_for_previous_command(pid_file, MAX_WAIT_PREV_S):
        return None

    # Create a PID file
    with pid_file.open('w') as f:
        f.write(str(os.getpid()))

    return pid_file


def main():
    # Setup logging
    logging.basicConfig(level=logging.INFO)

    desc = """
    Run command and cache its output. Return cached output if cache not expired.
    """
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("command", nargs="+")
    parser.add_argument("-c", "--cache-timeout", type=float, help=f"Cache timeout in seconds (float), default is {DEFAULT_CACHE_TIMEOUT_S}s")
    args = parser.parse_args()

    command = args.command
    cache_timeout = max(0.0, args.cache_timeout if args.cache_timeout else DEFAULT_CACHE_TIMEOUT_S)

    pid_file: Path = create_pid_file(command)
    # Timeout waiting for already running process
    try:
        if pid_file is None:
            print(f"ERROR: Process for given command still running: timeout ({MAX_WAIT_PREV_S}")
            sys.exit(2)
        if os.getpid() != int(pid_file.read_text()):
            print(f"ERROR: Creating PID file failed: Current process PID does not equal PID in the file.")
            sys.exit(2)
    except ValueError as e:
        print(f"ERROR: Creating PID file failed: {repr(e)}")
        sys.exit(2)
    except IOError as e:
        print(f"ERROR: Creating PID file failed: {repr(e)}")
        sys.exit(2)

    try:
        # Check cache and execute the command if needed
        cached_output, return_code = execute_or_get_cached_result(command, cache_timeout)

        stdout_ok = send_text_to_stdout(cached_output)

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
