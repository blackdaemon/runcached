#! /usr/bin/env -S python3 -O -u
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
import fcntl
import os
import random
import signal
import subprocess
import sys
import time
from collections.abc import Sequence, Callable
from contextlib import suppress, contextmanager
from hashlib import md5
from pathlib import Path
from shutil import copyfileobj
from subprocess import Popen
from typing import Iterable, Optional

# Avoid importing psutil if not necessary
if Path("/proc").is_dir():
    pid_exists = lambda pid: Path(f"/proc/{pid}").exists()
else:
    from psutil import pid_exists

# Configurable parameters
DEFAULT_CACHE_TIMEOUT_SEC: float = 20
MAX_WAIT_PREV_SEC: int = 5
MIN_RAND_SEC: float = 0
MAX_RAND_SEC: float = 0

os.environ['PYTHONUNBUFFERED'] = '1'


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


class CommandCache(object):
    def __init__(self, command:Sequence[str], cache_timeout:float):
        self.cache_timeout = cache_timeout
        self.command = command
        self.cache_dir = get_cache_dir()
        self.command_hash = generate_command_hash(command)
        self.output_cache = Path(self.cache_dir, f"{self.command_hash}.data")
        self.exit_file = Path(self.cache_dir, f"{self.command_hash}.exit")
        self.cmd_file = Path(self.cache_dir, f"{self.command_hash}.cmd")
        if not self.cmd_file.is_file():
            with self.cmd_file.open('w') as f:
                f.write(" ".join(self.command))

    def is_valid(self, cache_timeout: Optional[float] = None):
        if cache_timeout is None:
            cache_timeout = self.cache_timeout
        return self.output_cache.is_file() and time.time() - self.output_cache.stat().st_mtime <= cache_timeout

    def invalidate(self):
        self.output_cache.unlink()

    def cache_result(self, f: Iterable[str]):
        output_cache_file_encoding = "utf-8"
        with self.output_cache.open('w', encoding=output_cache_file_encoding) as f_cache:
            try:
                while True:
                    line = next(f)
                    f_cache.write(line)
                    f_cache.flush()
            except StopIteration as s:
                return_code = s.value
                # print(f"{return_code=}")
            except BrokenPipeError:
                return_code = 0
                # print(f"{return_code=}")
            except KeyboardInterrupt:
                return_code = 0
                # print(f"{return_code=}")
                raise
            finally:
                with self.exit_file.open('w') as f:
                    f.write(str(return_code))

                # Must update the modification timestamp so that the command runtime does not add to cache expiration timeout
                self.output_cache.touch()

        return return_code


def execute_command(command: Sequence[str], output_cache_file_encoding: str = "utf-8") -> Iterable[str]:
    """
    Execute a command and redirect results into cache files:

        - execution return_code into exit_file
        - stdout & stderr into output_cache_file
    """
    return_code = 1
    keyboard_interrupt = False
    process = Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    while process.poll() is None:
        try:
            if True: #process.stdout.readable():
                # print("Reading the line from command output...", file=sys.stderr)
                line = process.stdout.readline().decode(output_cache_file_encoding)
                try:
                    process.stdout.flush()
                    if line:
                        print(line, end="", file=sys.stdout, flush=True)
                except BrokenPipeError:
                    # print("BrokenPipeError...", file=sys.stderr)
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, sys.stdout.fileno())
                    process.send_signal(signal.SIGPIPE)
                    return_code = 0
                    break
                else:
                    yield line
            else:
                break
                # time.sleep(.5)
        except KeyboardInterrupt:
            return_code = 1
            keyboard_interrupt = True
            break
    else:
        return_code = process.returncode

    if keyboard_interrupt:
        raise KeyboardInterrupt()
    else:
        return return_code


def wait_for_previous_command(pid_file: Path, wait_time_sec: int) -> bool:
    """
    Wait for a previous instance of the command to finish by checking for the existence
    of the given pid_file.

    Returns True if the pid_file disappears within a time limit, False on timeout.

    Also handles stale pid_file by checking if the process is still running. If it's not running,
    the pid_file is removed and function returns True.

    :returns: Return True on success, False on timeout
    """
    if not pid_file.is_file():
        # PID file cleaned up, process finished
        return True

    for _ in range(wait_time_sec):
        time.sleep(1)
        if not pid_file.is_file():
            # PID file cleaned up, process finished
            return True
        # Check for stale PID file
        try:
            pid: int = int(pid_file.read_text())
        except (ValueError, IOError):
            # Corrupted or not readable PID file
            # Try to remove it anyway
            with suppress(IOError):
                pid_file.unlink()
            return False
        else:
            if not pid_exists(pid):
                # Stale PID file, remove it
                pid_file.unlink()
                return True
    else:
        print("ERROR: Timeout waiting for previous command to finish.")
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
            copyfileobj(f, sys.stdout.buffer)
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
    if MAX_RAND_SEC - MIN_RAND_SEC > 0:
        time.sleep(random.uniform(MIN_RAND_SEC, MAX_RAND_SEC))

    # Avoid parallel execution
    if not wait_for_previous_command(pid_file, MAX_WAIT_PREV_SEC):
        return None

    # Create a PID file
    with pid_file.open('w') as f:
        f.write(str(os.getpid()))

    return pid_file


def main():
    """
    Run command and cache its output. Return cached output if cache not expired.
    """

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("command", metavar="command ...", help="Command with arguments")
    parser.add_argument("-c", "--cache-timeout", type=float,
                        help=f"Cache timeout in seconds (float), default is {DEFAULT_CACHE_TIMEOUT_SEC}s")
    parser.add_argument("-e", "--cache-on-error", default=False, action="store_true",
                        help="Cache the command result also if it returns nonzero error code")
    parser.add_argument("-a", "--cache-on-abort", default=False, action="store_true",
                        help="Cache the command result also on ^C keyboard interrupt")
    parser.add_argument("-v", "--verbose", default=False, action="store_true",
                        help="Print diagnostic information")
    parser.add_argument('command_args', help=argparse.SUPPRESS, nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = [args.command] + args.command_args
    cache_timeout = max(0.0, args.cache_timeout if args.cache_timeout is not None else DEFAULT_CACHE_TIMEOUT_SEC)

    pid_file: Path = create_pid_file(command)
    # Timeout waiting for already running process
    try:
        if pid_file is None:
            print(f"ERROR: Process for given command still running: timeout ({MAX_WAIT_PREV_SEC}")
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
        cache = CommandCache(command, cache_timeout=cache_timeout)

        # Execute and cache the result
        # Output cache file encoding must be the same as sys.stdout encoding
        cache_file_encoding = sys.stdout.encoding
        if not cache_file_encoding:
            cache_file_encoding = "utf-8"

        # If cache is still valid, return cached result
        if cache.is_valid(cache_timeout):
            if args.verbose:
                print("DIAG: Returning cached result")
            stdout_ok = send_text_to_stdout(cache.output_cache)
            # TODO: (pavel) 13/12/2024 Provide correct value
            return_code = int(cache.exit_file.read_text())
        else:
            # Check cache and execute the command if needed
            if args.verbose:
                print("DIAG: Executing command")
            executor = execute_command(command, output_cache_file_encoding=cache_file_encoding)
            return_code = cache.cache_result(executor)

        if return_code !=0 and not args.cache_on_error:
            with suppress(IOError):
                if args.verbose:
                    print("DIAG: Destroying output cache (RC)")
                cache.invalidate()

        # TODO: (pavel) 13/12/2024 Fix stdout_ok
        if True:
        # if stdout_ok:
           sys.exit(return_code)
        else:
            # On pipe-broken error
            sys.exit(1)
    except (KeyboardInterrupt, Exception) as e:
            # Cleanup on error and remove cache files
            # print(isinstance(e, KeyboardInterrupt))
            # print(f"{args.cache_on_error=}; {args.cache_on_abort=}")
            if args.cache_on_error or isinstance(e, KeyboardInterrupt) and args.cache_on_abort:
                raise
            if args.verbose:
                print("DIAG: Destroying output cache (KI)")
            with suppress(IOError):
                cache.invalidate()
            raise
    finally:
        # Cleanup the PID file
        if pid_file.is_file():
            pid_file.unlink()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt) as e:
        # raise
        ...
        # Suppress traceback on specific interrupts
        # print(e)
