"""WebSocket client for xahaud ledger streaming.

This module provides async WebSocket functionality for subscribing
to ledger close events and other streams from xahaud nodes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.exceptions import WebSocketException

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


class WebSocketClient:
    """WebSocket client for subscribing to xahaud streams.

    Attributes:
        base_port_ws: Base WebSocket port (node N uses base + N)
        open_timeout: Connection timeout in seconds
    """

    def __init__(
        self,
        base_port_ws: int = 6005,
        open_timeout: float = 2.0,
    ) -> None:
        """Initialize the WebSocket client.

        Args:
            base_port_ws: Base WebSocket port (node N uses base + N)
            open_timeout: Connection timeout in seconds
        """
        self.base_port_ws = base_port_ws
        self.open_timeout = open_timeout

    def _get_url(self, node_id: int) -> str:
        """Get the WebSocket URL for a node."""
        port = self.base_port_ws + node_id
        return f"ws://127.0.0.1:{port}"

    async def subscribe_ledger_stream(
        self,
        node_id: int,
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to ledger close events from a node.

        Yields ledger close events as they occur. Automatically
        reconnects on connection failure.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Yields:
            Ledger close event dicts
        """
        url = self._get_url(node_id)

        while True:
            try:
                async with websockets.connect(
                    url, open_timeout=self.open_timeout
                ) as websocket:
                    # Subscribe to ledger stream
                    subscribe_msg = {
                        "id": 1,
                        "command": "subscribe",
                        "streams": ["ledger"],
                    }
                    await websocket.send(json.dumps(subscribe_msg))

                    # Wait for subscription response
                    await websocket.recv()

                    # Yield ledger close events
                    while True:
                        message = await websocket.recv()
                        data = json.loads(message)

                        if data.get("type") == "ledgerClosed":
                            yield data

            except (WebSocketException, ConnectionRefusedError, OSError) as e:
                logger.debug(f"WebSocket error on node {node_id}: {e}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Unexpected WebSocket error on node {node_id}: {e}")
                await asyncio.sleep(1)

    async def wait_for_ledger_close(
        self,
        node_id: int,
        target_index: int,
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Wait for a specific ledger to close on a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            target_index: Minimum ledger index to wait for
            timeout: Maximum time to wait in seconds

        Returns:
            Ledger close event dict, or None if timeout
        """
        url = self._get_url(node_id)

        try:
            async with asyncio.timeout(timeout):
                async with websockets.connect(
                    url, open_timeout=self.open_timeout
                ) as websocket:
                    # Subscribe to ledger stream
                    subscribe_msg = {
                        "id": 1,
                        "command": "subscribe",
                        "streams": ["ledger"],
                    }
                    await websocket.send(json.dumps(subscribe_msg))
                    await websocket.recv()  # Subscription response

                    # Wait for target ledger
                    while True:
                        message = await websocket.recv()
                        data = json.loads(message)

                        if data.get("type") == "ledgerClosed":
                            ledger_index = data.get("ledger_index", 0)
                            if ledger_index >= target_index:
                                return data

        except TimeoutError:
            logger.debug(f"Timeout waiting for ledger {target_index} on node {node_id}")
            return None
        except (WebSocketException, ConnectionRefusedError, OSError) as e:
            logger.debug(f"Connection error waiting for ledger on node {node_id}: {e}")
            return None

    async def wait_for_all_nodes_ledger_close(
        self,
        node_count: int,
        target_index: int,
        timeout: float = 10.0,
    ) -> dict[int, dict[str, Any]]:
        """Wait for all nodes to report a ledger close.

        Args:
            node_count: Number of nodes
            target_index: Minimum ledger index to wait for
            timeout: Maximum time to wait in seconds

        Returns:
            Dict mapping node_id -> ledger_event for nodes that reported
        """

        async def listen_node(node_id: int) -> tuple[int, dict[str, Any] | None]:
            """Listen to a single node."""
            event = await self.wait_for_ledger_close(node_id, target_index, timeout)
            return (node_id, event)

        # Create tasks for all nodes
        tasks = [
            asyncio.create_task(listen_node(node_id)) for node_id in range(node_count)
        ]

        # Wait for all tasks
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect successful results
        ledger_events: dict[int, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, tuple):
                node_id, event = result
                if event is not None:
                    ledger_events[node_id] = event

        return ledger_events

    async def check_connection(self, node_id: int) -> bool:
        """Check if we can connect to a node's WebSocket.

        Args:
            node_id: The node ID (0, 1, 2, etc.)

        Returns:
            True if connection succeeded, False otherwise
        """
        url = self._get_url(node_id)

        try:
            async with websockets.connect(url, open_timeout=self.open_timeout):
                return True
        except (WebSocketException, ConnectionRefusedError, OSError):
            return False
