"""Mock webhook receiver for xahaud subscription events.

Runs a simple HTTP server that receives POST requests from xahaud's
outbound webhook subscription system and logs them with Rich formatting.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import click
from rich.console import Console
from rich.text import Text

console = Console()


@dataclass
class ErrorConfig:
    """Configurable error response weights.

    Maps HTTP status codes to probability weights. On each request,
    rolls random.random() and walks cumulative weights to decide
    whether to return an error or 200.
    """

    weights: dict[int, float] = field(default_factory=dict)

    @classmethod
    def from_specs(cls, specs: tuple[str, ...]) -> ErrorConfig:
        """Parse --error flags like '500:0.25' into an ErrorConfig."""
        weights: dict[int, float] = {}
        for spec in specs:
            if ":" not in spec:
                raise click.BadParameter(
                    f"Invalid error spec: {spec!r}. Use STATUS:WEIGHT (e.g. 500:0.25)"
                )
            code_str, weight_str = spec.split(":", 1)
            try:
                code = int(code_str)
                weight = float(weight_str)
            except ValueError as e:
                raise click.BadParameter(
                    f"Invalid error spec: {spec!r}. Use STATUS:WEIGHT (e.g. 500:0.25)"
                ) from e
            if not (0 < weight <= 1):
                raise click.BadParameter(
                    f"Weight must be between 0 and 1 (exclusive), got {weight}"
                )
            weights[code] = weight

        total = sum(weights.values())
        if total > 1.0:
            raise click.BadParameter(f"Total error weights ({total:.0%}) exceed 100%")
        return cls(weights=weights)

    def roll(self) -> int:
        """Roll for a status code. Returns 200 if no error triggered."""
        if not self.weights:
            return 200
        r = random.random()
        cumulative = 0.0
        for code, weight in self.weights.items():
            cumulative += weight
            if r < cumulative:
                return code
        return 200

    def describe(self) -> str:
        """Human-readable description of error config."""
        if not self.weights:
            return "200 (100%)"
        parts = [f"{code} ({weight:.0%})" for code, weight in self.weights.items()]
        success_weight = 1.0 - sum(self.weights.values())
        parts.append(f"200 ({success_weight:.0%})")
        return ", ".join(parts)


@dataclass
class ServerStats:
    """Track request statistics."""

    total_requests: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)

    def record(self, status_code: int) -> None:
        self.total_requests += 1
        self.status_counts[status_code] = self.status_counts.get(status_code, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        uptime = time.time() - self.start_time
        return {
            "total_requests": self.total_requests,
            "status_counts": self.status_counts,
            "uptime_seconds": round(uptime, 1),
        }


class HooksRequestHandler(BaseHTTPRequestHandler):
    """Handle incoming webhook POST requests from xahaud."""

    error_config: ErrorConfig
    stats: ServerStats

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Decide status code
        status_code = self.error_config.roll()
        self.stats.record(status_code)

        # Parse and log
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {"_raw": body.decode("utf-8", errors="replace")}

        self._log_event(data, status_code)

        # Send response
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {"status": status_code}
        self.wfile.write(json.dumps(response).encode())

    def do_GET(self) -> None:
        if self.path == "/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.stats.to_dict(), indent=2).encode())
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')

    def _log_event(self, data: dict[str, Any], status_code: int) -> None:
        """Log received event with Rich formatting."""
        seq = data.get("seq", "?")
        method = data.get("method", "?")

        # Extract summary from params
        params = data.get("params", [])
        summary = ""
        if params and isinstance(params, list) and len(params) > 0:
            p = params[0]
            if isinstance(p, dict):
                parts = []
                if "type" in p:
                    parts.append(p["type"])
                if "stream" in p:
                    parts.append(f"stream={p['stream']}")
                if "ledger_index" in p:
                    parts.append(f"ledger={p['ledger_index']}")
                summary = " ".join(parts)

        # Build log line
        ts = time.strftime("%H:%M:%S")
        text = Text()
        text.append(f"{ts} ", style="dim")
        text.append(f"seq={seq} ", style="cyan")
        text.append(f"{method} ", style="bold")
        if summary:
            text.append(f"{summary} ", style="green")

        if status_code == 200:
            text.append(f"-> {status_code}", style="bold green")
        elif status_code >= 500:
            text.append(f"-> {status_code}", style="bold red")
        else:
            text.append(f"-> {status_code}", style="bold yellow")

        console.print(text)

        # Pretty-print the full JSON below
        if params:
            console.print_json(json.dumps(data), indent=2)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr request logging."""
        pass


def hooks_server_handler(
    host: str = "0.0.0.0",
    port: int = 8080,
    errors: tuple[str, ...] = (),
) -> None:
    """Run the mock webhook receiver server.

    Args:
        host: Bind address.
        port: Listen port.
        errors: Error specs as STATUS:WEIGHT strings.
    """
    error_config = ErrorConfig.from_specs(errors)
    stats = ServerStats()

    # Create handler class with config attached
    handler = partial(HooksRequestHandler)
    HooksRequestHandler.error_config = error_config
    HooksRequestHandler.stats = stats

    server = HTTPServer((host, port), handler)

    console.print(f"\nListening on [bold]http://{host}:{port}[/bold]")
    console.print(f"Error config: {error_config.describe()}")
    console.print(f"Stats endpoint: [bold]http://{host}:{port}/stats[/bold]")

    subscribe_cmd = json.dumps(
        {
            "command": "subscribe",
            "url": f"http://localhost:{port}",
            "streams": ["ledger", "transactions"],
        }
    )
    console.print("\nTo subscribe from xahaud RPC:")
    console.print(f"  {subscribe_cmd}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        console.print("\n[bold]Shutdown[/bold]")
        console.print(f"  Total requests: {stats.total_requests}")
        for code, count in sorted(stats.status_counts.items()):
            console.print(f"  {code}: {count}")
