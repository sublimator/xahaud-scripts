"""RPC client for communicating with xahaud nodes.

This module provides an HTTP RPC client using the requests library
for querying and controlling xahaud nodes.
"""

from __future__ import annotations

from typing import Any

import requests

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)

# Default timeout for RPC requests (seconds)
DEFAULT_TIMEOUT = 2.0


class RequestsRPCClient:
    """HTTP RPC client using the requests library.

    Attributes:
        base_port_rpc: Base RPC port (node N uses base + N)
        timeout: Request timeout in seconds
    """

    def __init__(
        self,
        base_port_rpc: int = 5005,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the RPC client.

        Args:
            base_port_rpc: Base RPC port (node N uses base + N)
            timeout: Request timeout in seconds
        """
        self.base_port_rpc = base_port_rpc
        self.timeout = timeout

    def _get_url(self, node_id: int) -> str:
        """Get the RPC URL for a node."""
        port = self.base_port_rpc + node_id
        return f"http://127.0.0.1:{port}"

    def _call(
        self,
        node_id: int,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make an RPC call to a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            method: RPC method name
            params: Optional parameters dict

        Returns:
            The result dict, or None if the call failed
        """
        url = self._get_url(node_id)
        payload = {
            "method": method,
            "params": [params or {}],
        }

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return data.get("result")
        except requests.exceptions.Timeout:
            logger.debug(f"RPC timeout for {method} on node {node_id}")
            return None
        except requests.exceptions.ConnectionError:
            logger.debug(f"Connection error for {method} on node {node_id}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"RPC error for {method} on node {node_id}: {e}")
            return None
        except ValueError as e:
            logger.warning(f"Invalid JSON response for {method} on node {node_id}: {e}")
            return None

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        """Get server_info from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The server_info result dict, or None if query failed
        """
        return self._call(node_id, "server_info")

    def server_definitions(self, node_id: int) -> dict[str, Any] | None:
        """Get server_definitions from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            The server_definitions result dict, or None if query failed
        """
        return self._call(node_id, "server_definitions")

    def peers(self, node_id: int) -> list[dict[str, Any]] | None:
        """Get peer list from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            List of peer dicts, or None if query failed
        """
        result = self._call(node_id, "peers")
        if result and "peers" in result:
            return result["peers"]
        return None

    def ledger(
        self,
        node_id: int,
        ledger_index: str | int = "validated",
        expand: bool = True,
        transactions: bool = False,
    ) -> dict[str, Any] | None:
        """Get ledger data from a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            ledger_index: Ledger index or "validated", "current", "closed"
            expand: If True, expand transaction details
            transactions: If True, include transactions

        Returns:
            The ledger result dict, or None if query failed
        """
        return self._call(
            node_id,
            "ledger",
            {
                "ledger_index": ledger_index,
                "expand": expand,
                "transactions": transactions,
            },
        )

    def ledger_entry(
        self,
        node_id: int,
        index: str,
    ) -> dict[str, Any] | None:
        """Get a specific ledger entry.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            index: Ledger entry index (hash)

        Returns:
            The ledger_entry result dict, or None if query failed
        """
        return self._call(node_id, "ledger_entry", {"index": index})

    def log_level(self, node_id: int, partition: str, severity: str) -> bool:
        """Set log level for a partition on a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            partition: Log partition name (e.g., "Validations")
            severity: Log severity (e.g., "trace", "debug", "info")

        Returns:
            True if successful, False otherwise
        """
        result = self._call(
            node_id,
            "log_level",
            {"severity": severity, "partition": partition},
        )
        return result is not None and result.get("status") == "success"

    def inject(self, node_id: int, tx_blob: str) -> dict[str, Any]:
        """Inject a transaction blob via RPC.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            tx_blob: Hex-encoded transaction blob

        Returns:
            The inject result dict (may indicate error)
        """
        result = self._call(node_id, "inject", {"tx_blob": tx_blob})
        return result or {"error": "RPC call failed"}

    def ping(self, node_id: int, inject: bool = False) -> dict[str, Any] | None:
        """Send a ping command to a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            inject: If True, include inject flag in ping

        Returns:
            The ping result dict, or None if query failed
        """
        params = {"inject": True} if inject else {}
        return self._call(node_id, "ping", params)

    def get_node_data(
        self,
        node_id: int,
        tracked_amendment: str | None = None,
    ) -> dict[str, Any]:
        """Get comprehensive data from a node for monitoring.

        Fetches both server_info and server_definitions in sequence.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            tracked_amendment: Optional amendment ID to check status

        Returns:
            Dict with 'node_id', 'server_info', 'amendment_status', 'error' keys
        """
        import time

        start_time = time.time()

        result: dict[str, Any] = {
            "node_id": node_id,
            "server_info": None,
            "amendment_status": None,
            "response_time": None,
            "error": None,
        }

        try:
            # Get server_info
            result["server_info"] = self.server_info(node_id)

            # Get server_definitions for amendment status
            if tracked_amendment:
                defs = self.server_definitions(node_id)
                if defs:
                    if "error" in defs:
                        result["amendment_status"] = {"status": "not_synced"}
                    elif "features" in defs:
                        features = defs["features"]
                        tracked_upper = tracked_amendment.upper()
                        if tracked_upper in features:
                            result["amendment_status"] = features[tracked_upper]
                        else:
                            result["amendment_status"] = {"status": "not_found"}

        except Exception as e:
            result["error"] = str(e)

        result["response_time"] = time.time() - start_time
        return result
