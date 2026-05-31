"""WebSocket endpoint and event broadcaster for real-time push.

The EventBroadcaster maintains a list of active WebSocket connections
and broadcasts events to all connected clients. Key service modules
(sync_service, trigger_service, monitor_service) call broadcast()
at critical points to push status updates to the frontend.
"""

import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class EventBroadcaster:
    """Maintains WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        logger.info("WebSocket client connected, total=%d", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)
        logger.info("WebSocket client disconnected, total=%d", len(self._connections))

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast an event to all connected clients."""
        if not self._connections:
            return

        message = json.dumps(
            {
                "type": event_type,
                "data": data,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            default=str,
        )

        stale: list[WebSocket] = []
        for ws in self._connections[:]:
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)

        for ws in stale:
            self.disconnect(ws)


# Global singleton broadcaster instance
broadcaster = EventBroadcaster()


@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint for real-time event streaming."""
    await broadcaster.connect(ws)
    try:
        while True:
            # Keep connection alive; client may send pings
            await ws.receive_text()
    except WebSocketDisconnect:
        broadcaster.disconnect(ws)
    except Exception:
        broadcaster.disconnect(ws)
