"""Process management for testnet.

This module provides utilities for finding and killing processes,
and checking if ports are listening.
"""

from __future__ import annotations

import subprocess

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


class UnixProcessManager:
    """Process manager for Unix-like systems (macOS, Linux).

    Uses pgrep/kill for process management and lsof/netstat for port checking.
    """

    def find_by_pattern(self, pattern: str) -> list[int]:
        """Find process IDs matching a pattern.

        Args:
            pattern: Pattern to match (used with pgrep -f)

        Returns:
            List of matching PIDs
        """
        try:
            logger.debug(f"Running: pgrep -f '{pattern}'")
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
            )
            logger.debug(
                f"pgrep returncode: {result.returncode}, stdout: '{result.stdout.strip()}', stderr: '{result.stderr.strip()}'"
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = [int(pid) for pid in result.stdout.strip().split("\n")]
                logger.info(f"Found {len(pids)} processes matching pattern: {pids}")
                return pids
            logger.debug("No processes found matching pattern")
            return []
        except (subprocess.SubprocessError, ValueError) as e:
            logger.warning(f"Error finding processes: {e}")
            return []

    def kill(self, pid: int, signal: int = 9) -> bool:
        """Kill a process by PID.

        Args:
            pid: Process ID to kill
            signal: Signal to send (default: 9 = SIGKILL)

        Returns:
            True if kill succeeded, False otherwise
        """
        try:
            result = subprocess.run(
                ["kill", f"-{signal}", str(pid)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.debug(f"Killed process {pid}")
                return True
            else:
                logger.warning(f"Failed to kill process {pid}: {result.stderr}")
                return False
        except subprocess.SubprocessError as e:
            logger.warning(f"Error killing process {pid}: {e}")
            return False

    def is_port_listening(self, port: int) -> bool:
        """Check if a port is currently listening.

        Tries lsof first (macOS), falls back to netstat.

        Args:
            port: Port number to check

        Returns:
            True if port is listening, False otherwise
        """
        # Try lsof first (more reliable on macOS)
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.debug(f"Port {port} is listening (lsof)")
                return True
            logger.debug(f"Port {port} is not listening (lsof)")
            return False
        except FileNotFoundError:
            pass  # lsof not available, try netstat
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout checking port {port} with lsof")
            return False
        except subprocess.SubprocessError as e:
            logger.warning(f"Error checking port {port} with lsof: {e}")

        # Fallback to netstat
        try:
            result = subprocess.run(
                ["netstat", "-an"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                # Look for the port in LISTEN state
                for line in result.stdout.split("\n"):
                    if f".{port} " in line and "LISTEN" in line:
                        logger.debug(f"Port {port} is listening (netstat)")
                        return True
            logger.debug(f"Port {port} is not listening (netstat)")
            return False
        except (
            FileNotFoundError,
            subprocess.TimeoutExpired,
            subprocess.SubprocessError,
        ) as e:
            logger.warning(f"Unable to check port {port}: {e}")
            return False

    def get_process_info(self, port: int) -> dict[str, str] | None:
        """Get information about the process listening on a port.

        Args:
            port: Port number to check

        Returns:
            Dict with 'pid' and 'process' keys, or None if not listening
        """
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                if len(lines) > 1:  # First line is header
                    parts = lines[1].split()
                    if len(parts) >= 2:
                        return {
                            "process": parts[0],
                            "pid": parts[1],
                        }
            return None
        except (
            FileNotFoundError,
            subprocess.TimeoutExpired,
            subprocess.SubprocessError,
        ):
            return None

    def get_port_state(self, port: int) -> list[dict[str, str]]:
        """Get all TCP connections using a port (any state).

        Catches LISTEN, TIME_WAIT, CLOSE_WAIT, ESTABLISHED, etc.

        Args:
            port: Port number to check

        Returns:
            List of dicts with 'process', 'pid', 'state' keys
        """
        results = []

        try:
            # lsof without state filter to catch all connections
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-P", "-n"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                for line in lines[1:]:  # Skip header
                    parts = line.split()
                    if len(parts) >= 10:
                        # lsof format: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
                        # NAME contains state in parentheses for TCP
                        name = parts[-1]
                        state = "UNKNOWN"
                        if "(" in name and ")" in name:
                            state = name.split("(")[-1].rstrip(")")
                        results.append(
                            {
                                "process": parts[0],
                                "pid": parts[1],
                                "state": state,
                            }
                        )
        except (
            FileNotFoundError,
            subprocess.TimeoutExpired,
            subprocess.SubprocessError,
        ):
            pass

        return results

    def check_ports_free(self, ports: list[int]) -> dict[int, list[dict[str, str]]]:
        """Check if ports are free, returning any that are in use.

        Args:
            ports: List of port numbers to check

        Returns:
            Dict mapping port -> list of connections (empty dict if all free)
        """
        in_use: dict[int, list[dict[str, str]]] = {}
        for port in ports:
            connections = self.get_port_state(port)
            if connections:
                in_use[port] = connections
        return in_use
