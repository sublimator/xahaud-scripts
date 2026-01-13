"""WebSocket client for xahaud ledger streaming.

This module provides async WebSocket functionality for subscribing
to ledger close events and other streams from xahaud nodes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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


@dataclass
class NodeConnectionState:
    """State for a single node's WebSocket connection."""

    node_id: int
    latest_event: dict[str, Any] | None = None
    last_event_time: float = 0.0
    connected: bool = False
    reconnect_attempts: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class PersistentWebSocketManager:
    """Manages persistent WebSocket connections to multiple nodes.

    Maintains one long-lived connection per node with automatic
    reconnection and keepalive. Events are buffered per-node so
    fresh data is always available without blocking.

    Usage:
        async with PersistentWebSocketManager(base_port_ws=6005, node_count=5) as mgr:
            await mgr.wait_until_ready()
            while True:
                events = mgr.get_latest_events()
                # Process events...
                await asyncio.sleep(0.1)
    """

    # Configuration
    PING_INTERVAL: float = 20.0  # Seconds between pings
    PING_TIMEOUT: float = 10.0  # Seconds to wait for pong
    RECONNECT_BASE_DELAY: float = 1.0  # Initial reconnect delay
    RECONNECT_MAX_DELAY: float = 30.0  # Maximum reconnect delay
    CONNECTION_TIMEOUT: float = 5.0  # Connection open timeout
    RECV_TIMEOUT: float = (
        30.0  # Max time to wait for a message (detect stale connections)
    )

    def __init__(
        self,
        base_port_ws: int = 6005,
        node_count: int = 5,
    ) -> None:
        """Initialize the manager.

        Args:
            base_port_ws: Base WebSocket port (node N uses base + N)
            node_count: Number of nodes to connect to
        """
        self.base_port_ws = base_port_ws
        self.node_count = node_count

        # Connection state per node
        self._nodes: dict[int, NodeConnectionState] = {}
        for node_id in range(node_count):
            self._nodes[node_id] = NodeConnectionState(node_id=node_id)

        # Lifecycle management
        self._running = False
        self._shutdown_event = asyncio.Event()

    def _get_url(self, node_id: int) -> str:
        """Get the WebSocket URL for a node."""
        port = self.base_port_ws + node_id
        return f"ws://127.0.0.1:{port}"

    async def __aenter__(self) -> PersistentWebSocketManager:
        """Start all connection tasks."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Stop all connection tasks."""
        await self.stop()

    async def start(self) -> None:
        """Start background connection tasks for all nodes."""
        if self._running:
            return

        self._running = True
        self._shutdown_event.clear()

        logger.info(
            f"Starting persistent WebSocket connections for {self.node_count} nodes"
        )

        for node_id in range(self.node_count):
            task = asyncio.create_task(
                self._node_connection_loop(node_id),
                name=f"ws-node-{node_id}",
            )
            self._nodes[node_id].task = task

    async def stop(self) -> None:
        """Stop all background tasks and close connections."""
        if not self._running:
            return

        logger.info("Stopping persistent WebSocket connections")
        self._running = False
        self._shutdown_event.set()

        # Cancel all tasks
        tasks = [state.task for state in self._nodes.values() if state.task is not None]

        for task in tasks:
            task.cancel()

        # Wait for tasks to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Clear state
        for state in self._nodes.values():
            state.task = None
            state.connected = False

    async def _node_connection_loop(self, node_id: int) -> None:
        """Background task that maintains connection to a single node.

        This loop:
        1. Connects to the node's WebSocket
        2. Subscribes to ledger stream
        3. Receives events and buffers the latest
        4. Reconnects on any failure with exponential backoff
        """
        state = self._nodes[node_id]
        url = self._get_url(node_id)

        while self._running:
            try:
                await self._connect_and_stream(node_id, url, state)
            except asyncio.CancelledError:
                logger.debug(f"Node {node_id}: connection task cancelled")
                raise
            except Exception as e:
                state.connected = False
                state.reconnect_attempts += 1

                # Exponential backoff with cap
                delay = min(
                    self.RECONNECT_BASE_DELAY
                    * (2 ** min(state.reconnect_attempts - 1, 5)),
                    self.RECONNECT_MAX_DELAY,
                )

                logger.debug(
                    f"Node {node_id}: connection failed ({e}), "
                    f"reconnecting in {delay:.1f}s (attempt {state.reconnect_attempts})"
                )

                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=delay,
                    )
                    # Shutdown requested during wait
                    break
                except TimeoutError:
                    # Normal timeout, continue to reconnect
                    pass

    async def _connect_and_stream(
        self,
        node_id: int,
        url: str,
        state: NodeConnectionState,
    ) -> None:
        """Connect to a node and stream ledger events.

        Raises on any connection or protocol error.
        """
        async with websockets.connect(
            url,
            open_timeout=self.CONNECTION_TIMEOUT,
            ping_interval=self.PING_INTERVAL,
            ping_timeout=self.PING_TIMEOUT,
        ) as websocket:
            # Subscribe to ledger stream
            subscribe_msg = {
                "id": 1,
                "command": "subscribe",
                "streams": ["ledger"],
            }
            await websocket.send(json.dumps(subscribe_msg))

            # Wait for subscription response
            response = await websocket.recv()
            response_data = json.loads(response)

            if "error" in response_data:
                raise RuntimeError(
                    f"Subscription failed: "
                    f"{response_data.get('error_message', response_data['error'])}"
                )

            # Mark as connected, reset reconnect counter
            state.connected = True
            state.reconnect_attempts = 0
            logger.debug(f"Node {node_id}: connected and subscribed")

            # Stream events until disconnection
            while self._running:
                try:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=self.RECV_TIMEOUT,
                    )
                except TimeoutError:
                    # No message for RECV_TIMEOUT seconds - connection may be stale
                    logger.debug(
                        f"Node {node_id}: no message for {self.RECV_TIMEOUT}s, reconnecting"
                    )
                    raise  # This will trigger reconnect

                data = json.loads(message)

                if data.get("type") == "ledgerClosed":
                    ledger_index = data.get("ledger_index", "?")
                    logger.debug(
                        f"Node {node_id}: received ledgerClosed #{ledger_index}"
                    )
                    state.latest_event = data
                    state.last_event_time = asyncio.get_event_loop().time()

    def get_latest_events(self) -> dict[int, dict[str, Any]]:
        """Get the latest ledger event from each connected node.

        Returns:
            Dict mapping node_id -> latest ledger event (only nodes with events)
        """
        return {
            node_id: state.latest_event
            for node_id, state in self._nodes.items()
            if state.latest_event is not None
        }

    def get_connection_status(self) -> dict[int, bool]:
        """Get connection status for all nodes.

        Returns:
            Dict mapping node_id -> connected (True/False)
        """
        return {node_id: state.connected for node_id, state in self._nodes.items()}

    def is_all_connected(self) -> bool:
        """Check if all nodes are connected."""
        return all(state.connected for state in self._nodes.values())

    def any_connected(self) -> bool:
        """Check if any node is connected."""
        return any(state.connected for state in self._nodes.values())

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostic info about all connections.

        Returns:
            Dict with connection states, latest ledger indices, and timing info
        """
        now = asyncio.get_event_loop().time()
        nodes_info = {}

        for node_id, state in self._nodes.items():
            latest_index = None
            time_since_event = None

            if state.latest_event:
                latest_index = state.latest_event.get("ledger_index")
                if state.last_event_time > 0:
                    time_since_event = round(now - state.last_event_time, 1)

            nodes_info[node_id] = {
                "connected": state.connected,
                "latest_index": latest_index,
                "time_since_event": time_since_event,
                "reconnect_attempts": state.reconnect_attempts,
            }

        return {
            "nodes": nodes_info,
            "any_connected": self.any_connected(),
            "all_connected": self.is_all_connected(),
        }

    async def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """Wait until at least one node is connected.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if at least one node connected, False on timeout
        """
        start = asyncio.get_event_loop().time()

        while not self.any_connected():
            if asyncio.get_event_loop().time() - start > timeout:
                return False
            await asyncio.sleep(0.1)

        return True

    async def wait_for_new_ledger(
        self,
        min_ledger_index: int,
        timeout: float = 15.0,
        all_nodes_timeout: float = 0.5,
    ) -> dict[int, dict[str, Any]]:
        """Wait until we have ledger events >= min_ledger_index from all nodes.

        This is the replacement for wait_for_all_nodes_ledger_close().
        It checks the buffered events rather than making new connections.

        Once the first node reports the target ledger, waits up to
        all_nodes_timeout for remaining nodes before returning.

        Args:
            min_ledger_index: Minimum ledger index to wait for
            timeout: Maximum time to wait for first event
            all_nodes_timeout: Time to wait for remaining nodes after first event

        Returns:
            Dict mapping node_id -> ledger event for nodes with sufficient index
        """
        start = asyncio.get_event_loop().time()
        collection_start: float | None = None

        while True:
            events = self.get_latest_events()

            # Filter events that meet the minimum ledger index
            matching = {
                node_id: event
                for node_id, event in events.items()
                if event.get("ledger_index", 0) >= min_ledger_index
            }

            now = asyncio.get_event_loop().time()

            if matching:
                # Start collection window on first match
                if collection_start is None:
                    collection_start = now

                # Check if we have all nodes or collection timeout expired
                if (
                    len(matching) >= self.node_count
                    or now - collection_start >= all_nodes_timeout
                ):
                    return matching

            # Check overall timeout (only before first match)
            if collection_start is None and now - start > timeout:
                return {}

            await asyncio.sleep(0.05)  # Poll faster during collection
