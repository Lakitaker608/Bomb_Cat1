"""五人制炸弹猫的 Python 服务层。

运行：
    python -m pip install -r requirements.txt
    python server.py

可选 Agent 配置（只在服务端保存）：
    set AGENT_ENDPOINT=https://your-agent.example.com/v1/game-turn
    set AGENT_TOKEN=your-server-side-token
"""
from __future__ import annotations

import json
import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

CARD_LIBRARY: dict[str, dict[str, str]] = {
    "defuse": {"name": "拆弹"}, "skip": {"name": "跳过"}, "favor": {"name": "索要"},
    "see": {"name": "预知未来"}, "shuffle": {"name": "洗牌"}, "attack": {"name": "攻击"},
    "taco": {"name": "墨西哥卷猫"}, "rainbow": {"name": "彩虹猫"}, "potato": {"name": "土豆猫"}, "braid_uncle": {"name": "辫子大叔"},
}
DRAW_POOL = tuple(CARD_LIBRARY)
BOT_NAMES = ("BombBot", "橘猫队长", "闪电爪", "夜行喵")
AGENT_ENDPOINT = os.getenv("AGENT_ENDPOINT", "").strip()
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "").strip()


@dataclass
class Seat:
    seat_id: str
    name: str
    is_human: bool = False
    hand: list[str] = field(default_factory=list)
    alive: bool = True

    def public_info(self) -> dict[str, Any]:
        return {"id": self.seat_id, "name": self.name, "cardCount": len(self.hand), "alive": self.alive, "isHuman": self.is_human}


@dataclass
class GameSession:
    game_id: str
    deck: int = 34
    seats: list[Seat] = field(default_factory=list)
    discard: str = "attack"
    turn_index: int = 0
    drawn_this_turn: bool = False
    logs: list[str] = field(default_factory=lambda: ["五人制对局开始：请先从牌库抽取第一张牌。"])

    @property
    def human(self) -> Seat:
        return self.seats[0]

    @property
    def current_seat(self) -> Seat:
        return self.seats[self.turn_index]

    def public_state(self) -> dict[str, Any]:
        return {
            "gameId": self.game_id,
            "mode": "five-player",
            "deck": self.deck,
            "playerHand": self.human.hand,
            "opponents": [seat.public_info() for seat in self.seats[1:]],
            "discard": self.discard,
            "turn": "player" if self.current_seat.is_human else "agent",
            "currentPlayerId": self.current_seat.seat_id,
            "currentPlayerName": self.current_seat.name,
            "canDraw": self.current_seat.is_human and not self.drawn_this_turn and self.deck > 0,
            "drawnThisTurn": self.drawn_this_turn,
            "drawCountNext": min(4, self.deck),
            "logs": self.logs[-30:],
        }


SESSIONS: dict[str, GameSession] = {}
SESSIONS_LOCK = threading.Lock()


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def get_session(game_id: str) -> GameSession | None:
    with SESSIONS_LOCK:
        return SESSIONS.get(game_id)


def add_log(session: GameSession, text: str) -> None:
    session.logs.append(f"{datetime.now().strftime('%H:%M')}|{text}")


def next_alive_turn(session: GameSession) -> None:
    for _ in range(len(session.seats)):
        session.turn_index = (session.turn_index + 1) % len(session.seats)
        if session.current_seat.alive:
            session.drawn_this_turn = False
            return


def agent_payload(session: GameSession, seat: Seat) -> dict[str, Any]:
    """仅发送 Agent 决策必需的公开状态，不泄露其他玩家的具体手牌。"""
    return {
        "game": "bomb-cat", "mode": "five-player", "gameId": session.game_id,
        "actor": seat.public_info(), "deckCount": session.deck,
        "players": [player.public_info() for player in session.seats],
        "discard": session.discard, "allowedActions": ["draw", "play"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def remote_agent_move(session: GameSession, seat: Seat) -> dict[str, str] | None:
    if not AGENT_ENDPOINT:
        return None
    headers = {"Content-Type": "application/json"}
    if AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    req = Request(AGENT_ENDPOINT, data=json.dumps(agent_payload(session, seat)).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=12) as response:
            move = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
    if not isinstance(move, dict) or move.get("action") not in {"draw", "play"}:
        return None
    return move


def apply_bot_turn(session: GameSession, seat: Seat) -> None:
    move = remote_agent_move(session, seat)
    # 演示 AI 有牌时偶尔出攻击，否则摸一张牌。
    if not move:
        move = {"action": "play", "cardId": "attack"} if seat.hand and random.random() > 0.68 else {"action": "draw"}
    card_id = move.get("cardId")
    if move.get("action") == "play" and card_id in seat.hand and card_id in CARD_LIBRARY:
        seat.hand.remove(card_id)
        session.discard = card_id
        add_log(session, f"{seat.name} 打出 {CARD_LIBRARY[card_id]['name']}。")
    elif session.deck > 0:
        card_id = random.choice(DRAW_POOL)
        seat.hand.append(card_id)
        session.deck -= 1
        add_log(session, f"{seat.name} 摸了一张牌。")
    else:
        add_log(session, f"{seat.name} 结束回合。")


def run_until_human(session: GameSession) -> None:
    """在人类出牌后，依次完成其余四名 AI 的回合，再回到玩家。"""
    next_alive_turn(session)
    while not session.current_seat.is_human:
        apply_bot_turn(session, session.current_seat)
        next_alive_turn(session)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(BASE_DIR, filename)


@app.post("/api/games")
def create_game():
    seats = [Seat("player", "你", is_human=True)] + [Seat(f"bot-{index + 1}", name) for index, name in enumerate(BOT_NAMES)]
    session = GameSession(game_id=uuid.uuid4().hex, seats=seats)
    with SESSIONS_LOCK:
        SESSIONS[session.game_id] = session
    return jsonify({"ok": True, "game": session.public_state()}), 201


@app.get("/api/games/<game_id>")
def read_game(game_id: str):
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    return jsonify({"ok": True, "game": session.public_state()})


@app.post("/api/games/<game_id>/draw")
def draw_card(game_id: str):
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    if session.drawn_this_turn:
        return fail("本回合已经抽过牌了，请出牌或结束回合。", 409)
    draw_count = min(4, session.deck)
    if draw_count <= 0:
        return fail("牌库已空，本局结束。", 409)
    card_ids = [random.choice(DRAW_POOL) for _ in range(draw_count)]
    session.deck -= draw_count
    session.human.hand.extend(card_ids)
    session.drawn_this_turn = True
    names = "、".join(CARD_LIBRARY[card_id]["name"] for card_id in card_ids)
    add_log(session, f"你一次抽取了 {draw_count} 张牌：{names}。")
    return jsonify({"ok": True, "drawnCards": card_ids, "game": session.public_state()})


@app.post("/api/games/<game_id>/play")
def play_card(game_id: str):
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    card_id = (request.get_json(silent=True) or {}).get("cardId")
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    if card_id not in CARD_LIBRARY or card_id not in session.human.hand:
        return fail("该卡牌不在你的手牌中。", 422)
    session.human.hand.remove(card_id)
    session.discard = card_id
    add_log(session, f"你打出 {CARD_LIBRARY[card_id]['name']}。")
    return jsonify({"ok": True, "game": session.public_state()})


@app.post("/api/games/<game_id>/end-turn")
def end_turn(game_id: str):
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    if not session.human.hand:
        return fail("请先抽取第一张牌。", 422)
    session.drawn_this_turn = False
    run_until_human(session)
    return jsonify({"ok": True, "game": session.public_state()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
