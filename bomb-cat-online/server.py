"""五人制炸弹猫的 Python 服务层。"""
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
from flask_cors import CORS  # 修复：补充导入 CORS
from zhipuai import ZhipuAI

api_key = os.getenv("ZHIPU_API_KEY")
if not api_key:
    raise RuntimeError("请在环境变量中设置 ZHIPU_API_KEY。")
client = ZhipuAI(api_key=api_key)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
CORS(app)
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
    logs: list[str] = field(default_factory=lambda: ["五人制对局开始：每人已抽取4张手牌。"])

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
            "canDraw": self.current_seat.is_human and self.deck > 0,
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
    
    # 演示 AI：如果有牌，有一定概率出攻击牌，否则选择抽牌
    if not move:
        if seat.hand and "attack" in seat.hand and random.random() > 0.5:
            move = {"action": "play", "cardId": "attack"}
        else:
            move = {"action": "draw"}

    card_id = move.get("cardId")
    if move.get("action") == "play" and card_id in seat.hand and card_id in CARD_LIBRARY:
        seat.hand.remove(card_id)
        session.discard = card_id
        add_log(session, f"{seat.name} 打出 {CARD_LIBRARY[card_id]['name']}。")
        
        # 出牌后抽一张牌
        if session.deck > 0:
            new_card = random.choice(DRAW_POOL)
            seat.hand.append(new_card)
            session.deck -= 1
            add_log(session, f"{seat.name} 摸了一张牌。")
    else:
        # 只抽一张牌
        if session.deck > 0:
            card_id = random.choice(DRAW_POOL)
            seat.hand.append(card_id)
            session.deck -= 1
            add_log(session, f"{seat.name} 摸了一张牌。")
        else:
            add_log(session, f"{seat.name} 结束回合（牌库已空）。")


def run_until_human(session: GameSession) -> None:
    """当前玩家操作完毕后，依次完成其余四名 AI 的回合，再回到玩家。"""
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
    
    # 核心修改：开局所有人抽四张牌
    for seat in session.seats:
        draw_count = min(4, session.deck)
        if draw_count > 0:
            cards = [random.choice(DRAW_POOL) for _ in range(draw_count)]
            seat.hand.extend(cards)
            session.deck -= draw_count
    add_log(session, "【开局发牌】所有玩家均已抽取4张初始手牌！")
    
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
    """核心修改：只抽一张牌，并结束回合"""
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    if session.deck <= 0:
        return fail("牌库已空，本局结束。", 409)

    # 摸一张牌
    card_id = random.choice(DRAW_POOL)
    session.human.hand.append(card_id)
    session.deck -= 1
    add_log(session, f"你从牌库抽取了 1 张牌：{CARD_LIBRARY[card_id]['name']}。")

    # 摸牌后直接结束回合，进入 AI 轮次
    run_until_human(session)
    return jsonify({"ok": True, "drawnCard": card_id, "game": session.public_state()})


@app.post("/api/games/<game_id>/play")
def play_card(game_id: str):
    """核心修改：出一张牌，自动抽一张牌，并结束回合"""
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    card_id = (request.get_json(silent=True) or {}).get("cardId")
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    if card_id not in CARD_LIBRARY or card_id not in session.human.hand:
        return fail("该卡牌不在你的手牌中。", 422)

    # 1. 打出牌
    session.human.hand.remove(card_id)
    session.discard = card_id
    add_log(session, f"你打出 {CARD_LIBRARY[card_id]['name']}。")

    # 2. 出牌后抽一张牌
    if session.deck > 0:
        new_card = random.choice(DRAW_POOL)
        session.human.hand.append(new_card)
        session.deck -= 1
        add_log(session, f"出牌后，你抽取了 1 张牌：{CARD_LIBRARY[new_card]['name']}。")
    else:
        add_log(session, "牌库已空，你无法抽牌。")

    # 3. 结束回合，进入 AI 轮次
    run_until_human(session)
    return jsonify({"ok": True, "game": session.public_state()})


@app.post("/api/games/<game_id>/end-turn")
def end_turn(game_id: str):
    """保留该接口以防前端报错，但新机制下只需出牌或抽牌即可自动结束回合。"""
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    return jsonify({"ok": True, "message": "新机制下无需手动结束回合", "game": session.public_state()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
