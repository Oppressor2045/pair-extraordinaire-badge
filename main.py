branch-1
"""
Real-Time Multiplayer Game Server
Built with FastAPI + WebSocket

Features:
  - Real-time WebSocket communication
  - Room-based multiplayer sessions
  - Player state synchronization
  - Chat system
  - Heartbeat / ping-pong keep-alive
  - REST API for lobby management

Run:
  pip install fastapi uvicorn
  uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="🎮 Multiplayer Game Server",
    description="Real-time multiplayer game server powered by FastAPI & WebSocket",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────
class Player:
    def __init__(self, player_id: str, nickname: str, websocket: WebSocket):
        self.player_id = player_id
        self.nickname = nickname
        self.websocket = websocket
        self.room_id: Optional[str] = None
        self.position: Dict = {"x": 0, "y": 0}
        self.score: int = 0
        self.is_alive: bool = True
        self.joined_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict:
        return {
            "player_id": self.player_id,
            "nickname": self.nickname,
            "room_id": self.room_id,
            "position": self.position,
            "score": self.score,
            "is_alive": self.is_alive,
        }


class GameRoom:
    MAX_PLAYERS = 4

    def __init__(self, room_id: str, host_id: str, room_name: str):
        self.room_id = room_id
        self.room_name = room_name
        self.host_id = host_id
        self.players: Dict[str, Player] = {}
        self.is_started: bool = False
        self.created_at: str = datetime.utcnow().isoformat()
        self.chat_history: list = []

    @property
    def is_full(self) -> bool:
        return len(self.players) >= self.MAX_PLAYERS

    def to_dict(self) -> Dict:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "host_id": self.host_id,
            "player_count": len(self.players),
            "max_players": self.MAX_PLAYERS,
            "is_started": self.is_started,
            "is_full": self.is_full,
            "players": [p.to_dict() for p in self.players.values()],
            "created_at": self.created_at,
        }


# REST API request schemas
class CreateRoomRequest(BaseModel):
    room_name: str
    host_nickname: str


# ─────────────────────────────────────────
# In-Memory Game State
# ─────────────────────────────────────────
players: Dict[str, Player] = {}       # player_id -> Player
rooms: Dict[str, GameRoom] = {}       # room_id   -> GameRoom


# ─────────────────────────────────────────
# Broadcast Helpers
# ─────────────────────────────────────────
async def broadcast_to_room(room_id: str, message: Dict, exclude: Optional[str] = None):
    """Send a message to all players in a room."""
    room = rooms.get(room_id)
    if not room:
        return
    payload = json.dumps(message)
    dead: list[str] = []
    for pid, player in room.players.items():
        if pid == exclude:
            continue
        try:
            await player.websocket.send_text(payload)
        except Exception:
            dead.append(pid)
    for pid in dead:
        await _remove_player(pid)


async def send_to_player(player_id: str, message: Dict):
    """Send a message to a single player."""
    player = players.get(player_id)
    if player:
        try:
            await player.websocket.send_text(json.dumps(message))
        except Exception:
            await _remove_player(player_id)


# ─────────────────────────────────────────
# Player Lifecycle
# ─────────────────────────────────────────
async def _remove_player(player_id: str):
    """Clean up a disconnected player."""
    player = players.pop(player_id, None)
    if not player:
        return

    room_id = player.room_id
    if room_id and room_id in rooms:
        room = rooms[room_id]
        room.players.pop(player_id, None)

        await broadcast_to_room(room_id, {
            "type": "player_left",
            "player_id": player_id,
            "nickname": player.nickname,
            "player_count": len(room.players),
        })

        # Promote a new host if needed
        if room.players and room.host_id == player_id:
            new_host_id = next(iter(room.players))
            room.host_id = new_host_id
            await broadcast_to_room(room_id, {
                "type": "host_changed",
                "new_host_id": new_host_id,
            })

        # Delete empty rooms
        if not room.players:
            rooms.pop(room_id, None)
            logger.info(f"Room {room_id} deleted (empty)")

    logger.info(f"Player {player.nickname} ({player_id}) disconnected")


# ─────────────────────────────────────────
# WebSocket Message Handlers
# ─────────────────────────────────────────
async def handle_join_room(player: Player, data: Dict):
    room_id = data.get("room_id")
    room = rooms.get(room_id)

    if not room:
        await send_to_player(player.player_id, {"type": "error", "message": "Room not found"})
        return
    if room.is_full:
        await send_to_player(player.player_id, {"type": "error", "message": "Room is full"})
        return
    if room.is_started:
        await send_to_player(player.player_id, {"type": "error", "message": "Game already started"})
        return

    # Leave current room if any
    if player.room_id:
        await handle_leave_room(player, {})

    room.players[player.player_id] = player
    player.room_id = room_id

    await send_to_player(player.player_id, {
        "type": "joined_room",
        "room": room.to_dict(),
        "chat_history": room.chat_history[-20:],
    })

    await broadcast_to_room(room_id, {
        "type": "player_joined",
        "player": player.to_dict(),
        "player_count": len(room.players),
    }, exclude=player.player_id)

    logger.info(f"{player.nickname} joined room '{room.room_name}' ({room_id})")


async def handle_leave_room(player: Player, data: Dict):
    if not player.room_id:
        return
    await _remove_player(player.player_id)
    # Re-register player (not fully disconnected, just left room)
    players[player.player_id] = player
    player.room_id = None


async def handle_game_start(player: Player, data: Dict):
    room = rooms.get(player.room_id)
    if not room:
        return
    if room.host_id != player.player_id:
        await send_to_player(player.player_id, {"type": "error", "message": "Only the host can start"})
        return
    if len(room.players) < 1:
        await send_to_player(player.player_id, {"type": "error", "message": "Not enough players"})
        return

    room.is_started = True
    await broadcast_to_room(player.room_id, {
        "type": "game_started",
        "players": [p.to_dict() for p in room.players.values()],
        "timestamp": datetime.utcnow().isoformat(),
    })
    logger.info(f"Game started in room {player.room_id}")


async def handle_player_move(player: Player, data: Dict):
    if not player.room_id:
        return
    position = data.get("position", {})
    player.position = {
        "x": float(position.get("x", player.position["x"])),
        "y": float(position.get("y", player.position["y"])),
    }
    await broadcast_to_room(player.room_id, {
        "type": "player_moved",
        "player_id": player.player_id,
        "position": player.position,
    }, exclude=player.player_id)


async def handle_game_action(player: Player, data: Dict):
    if not player.room_id:
        return
    action = data.get("action")
    payload = data.get("payload", {})
    await broadcast_to_room(player.room_id, {
        "type": "game_action",
        "player_id": player.player_id,
        "action": action,
        "payload": payload,
        "timestamp": datetime.utcnow().isoformat(),
    }, exclude=player.player_id)


async def handle_score_update(player: Player, data: Dict):
    delta = int(data.get("delta", 0))
    player.score = max(0, player.score + delta)
    if player.room_id:
        await broadcast_to_room(player.room_id, {
            "type": "score_updated",
            "player_id": player.player_id,
            "score": player.score,
        })


async def handle_chat(player: Player, data: Dict):
    if not player.room_id:
        return
    room = rooms.get(player.room_id)
    if not room:
        return

    msg = {
        "type": "chat_message",
        "player_id": player.player_id,
        "nickname": player.nickname,
        "message": str(data.get("message", ""))[:200],
        "timestamp": datetime.utcnow().isoformat(),
    }
    room.chat_history.append(msg)
    if len(room.chat_history) > 100:
        room.chat_history = room.chat_history[-100:]

    await broadcast_to_room(player.room_id, msg)


# ─────────────────────────────────────────
# WebSocket Endpoint
# ─────────────────────────────────────────
MESSAGE_HANDLERS = {
    "join_room":    handle_join_room,
    "leave_room":   handle_leave_room,
    "game_start":   handle_game_start,
    "player_move":  handle_player_move,
    "game_action":  handle_game_action,
    "score_update": handle_score_update,
    "chat":         handle_chat,
}


@app.websocket("/ws/{nickname}")
async def websocket_endpoint(websocket: WebSocket, nickname: str):
    await websocket.accept()

    player_id = str(uuid.uuid4())[:8]
    player = Player(player_id=player_id, nickname=nickname, websocket=websocket)
    players[player_id] = player

    logger.info(f"Player connected: {nickname} ({player_id})")

    await send_to_player(player_id, {
        "type": "connected",
        "player_id": player_id,
        "nickname": nickname,
        "message": f"Welcome, {nickname}! 🎮",
        "available_rooms": [r.to_dict() for r in rooms.values() if not r.is_started],
    })

    try:
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await send_to_player(player_id, {"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            # Ping / pong keep-alive
            if msg_type == "ping":
                await send_to_player(player_id, {"type": "pong", "timestamp": datetime.utcnow().isoformat()})
                continue

            handler = MESSAGE_HANDLERS.get(msg_type)
            if handler:
                await handler(player, data)
            else:
                await send_to_player(player_id, {"type": "error", "message": f"Unknown message type: {msg_type}"})

    except asyncio.TimeoutError:
        logger.warning(f"Player {nickname} timed out")
    except WebSocketDisconnect:
        logger.info(f"Player {nickname} disconnected normally")
    except Exception as e:
        logger.error(f"Unexpected error for {nickname}: {e}")
    finally:
        await _remove_player(player_id)


# ─────────────────────────────────────────
# REST API – Lobby
# ─────────────────────────────────────────
@app.get("/", tags=["Health"])
async def root():
    return {
        "status": "ok",
        "server": "🎮 Multiplayer Game Server",
        "version": "1.0.0",
        "connected_players": len(players),
        "active_rooms": len(rooms),
    }


@app.get("/rooms", tags=["Lobby"])
async def list_rooms():
    """List all open (not yet started) game rooms."""
    return {
        "rooms": [r.to_dict() for r in rooms.values() if not r.is_started],
        "total": len(rooms),
    }


@app.post("/rooms", tags=["Lobby"], status_code=201)
async def create_room(req: CreateRoomRequest):
    """Create a new game room via REST (pre-lobby use)."""
    room_id = str(uuid.uuid4())[:8]
    host_id = f"rest-{str(uuid.uuid4())[:6]}"  # placeholder until WS host joins
    room = GameRoom(room_id=room_id, host_id=host_id, room_name=req.room_name)
    rooms[room_id] = room
    logger.info(f"Room created: '{req.room_name}' ({room_id}) by {req.host_nickname}")
    return {"room_id": room_id, "room": room.to_dict()}


@app.get("/rooms/{room_id}", tags=["Lobby"])
async def get_room(room_id: str):
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room.to_dict()


@app.delete("/rooms/{room_id}", tags=["Lobby"])
async def delete_room(room_id: str):
    room = rooms.pop(room_id, None)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    await broadcast_to_room(room_id, {"type": "room_closed", "message": "Room was closed by admin"})
    return {"message": f"Room {room_id} deleted"}


@app.get("/players", tags=["Players"])
async def list_players():
    return {
        "players": [p.to_dict() for p in players.values()],
        "total": len(players),
    }


# ─────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")


main
