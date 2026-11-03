"""五人制炸弹猫的 Python 服务层。"""
from __future__ import annotations

import json
import os
import random
import math
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
    "exploding_kitten": {"name": "炸弹猫"},
}
# 普通抽卡池（不包含炸弹和拆弹）
NORMAL_DRAW_POOL = ("skip", "favor", "see", "shuffle", "attack", "taco", "rainbow", "potato", "braid_uncle")
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
    extra_turns: int = 0

    def public_info(self) -> dict[str, Any]:
        return {"id": self.seat_id, "name": self.name, "cardCount": len(self.hand), "alive": self.alive, "isHuman": self.is_human}

@dataclass
class GameSession:
    game_id: str
    deck: list[str] = field(default_factory=list) # 现在是真实的卡牌列表
    seats: list[Seat] = field(default_factory=list)
    discard: str = ""
    turn_index: int = 0
    drawn_this_turn: bool = False
    extra_turns: int t = 0
    game_over: bool = False
    winner_id: str | None = None
    logs: list[str] = field(default_factory=lambda: ["五人制对局开始！"])

    @property
    def human(self) -> Seat:
        return self.seats[0]

    @property
    def current_seat(self) -> Seat:
        return self.seats[self.turn_index]

    def public_state(self) -> dict[str, Any]:
        alive_count = sum(1 for s in self.seats if s.alive)
        return {
            "gameId": self.game_id,
            "mode": "five-player",
            "deck": len(self.deck),
            "playerHand": self.human.hand,
            "opponents": [seat.public_info() for seat in self.seats[1:]],
            "discard": self.discard,
            "turn": "player" if self.current_seat.is_human else "agent",
            "currentPlayerId": self.current_seat.seat_id,
            "currentPlayerName": self.current_seat.name,
            "canDraw": self.current_seat.is_human and not self.drawn_this_turn and len(self.deck) > 0,
            "drawnThisTurn": self.drawn_this_turn,
            "drawCountNext": 1, # 强制前端显示抽 1 张
            "bombsRemaining": bombs,
            "gameOver": self.game_over,
            "playerAlive": self.human.alive,
            "winnerId": self.winner_id,
            "logs": self.logs[-30:]
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
    return {
        "game": "bomb-cat", "mode": "five-player", "gameId": session.game_id,
        "actor": seat.public_info(), "deckCount": len(session.deck),
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

def next_alive_turn(session: GameSession) -> None:
    # 如果存在额外回合（被攻击），则当前玩家不换，继续行动
    if session.extra_turns > 0:
        session.extra_turns -= 1
        session.drawn_this_turn = False
        return
        
    # 正常轮转下一个存活玩家
    for _ in range(len(session.seats)):
        session.turn_index = (session.turn_index + 1) % len(session.seats)
        if session.current_seat.alive:
            session.drawn_this_turn = False
            return
            
def check_win_condition(session: GameSession) -> None:
    """检查游戏是否只剩一名存活玩家"""
    alive_seats = [seat for seat in session.seats if seat.alive]
    if len(alive_seats) <= 1:
        session.game_over = True
        if alive_seats:
            session.winner_id = alive_seats[0].seat_id
            add_log(session, f"🏆 游戏结束！{alive_seats[0].name} 获得了最终胜利！")
        else:
            add_log(session, "💀 游戏结束！所有玩家同归于尽，平局。")


def apply_bot_turn(session: GameSession, seat: Seat) -> None:
    if not seat.alive or session.game_over:
        return

    move = remote_agent_move(session, seat)
    
    if not move:
        if "attack" in seat.hand and random.random() > 0.7:
            move = {"action": "play", "cardId": "attack"}
        elif "skip" in seat.hand and random.random() > 0.8:
            move = {"action": "play", "cardId": "skip"}
        else:
            move = {"action": "draw"}

    card_id = move.get("cardId")

    # 1. AI 出牌逻辑
    if move.get("action") == "play" and card_id in seat.hand and card_id in CARD_LIBRARY:
        seat.hand.remove(card_id)
        session.discard = card_id
        add_log(session, f"{seat.name} 打出 {CARD_LIBRARY[card_id]['name']}。")
        
        if card_id == "attack":
            session.extra_turns += 1
            add_log(session, f"{seat.name} 的攻击生效，下一个玩家将承受额外回合！")
        elif card_id == "skip":
            add_log(session, f"{seat.name} 使用了跳过，结束回合。")
            return  # 跳过回合，不抽牌直接返回
            
    elif session.deck:
        card = session.deck.pop(0)
        if card == "exploding_kitten":
            if "defuse" in seat.hand:
                # AI 成功拆弹
                seat.hand.remove("defuse")
                add_log(session, f"{seat.name} 抽到了炸弹猫，但使用了【拆弹】成功化解。")
                # 炸弹放回牌堆（不能放最顶端）
                insert_pos = random.randint(1, len(session.deck)) if len(session.deck) > 1 else 0
                session.deck.insert(insert_pos, card)
            else:
                # AI 被炸死
                seat.alive = False
                add_log(session, f"💥 {seat.name} 抽到了炸弹猫且没有拆弹，被炸死淘汰！")
                # 将死亡玩家的手牌收回牌堆并打乱
                session.deck.extend(seat.hand)
                seat.hand.clear()
                random.shuffle(session.deck)
                check_win_condition(session)
        else:
            # 抽到普通牌
            seat.hand.append(card)
            add_log(session, f"{seat.name} 摸了一张牌。")
    else:
        add_log(session, f"牌堆已空，{seat.name} 无法摸牌。")

def run_until_human(session: GameSession) -> None:
    next_alive_turn(session)
    while not session.current_seat.is_human and sum(1 for s in session.seats if s.alive) > 1:
        apply_bot_turn(session, session.current_seat)
        # 如果 AI 摸牌了或者出攻击/跳过牌强制结束了回合，则尝试流转
        if session.drawn_this_turn:
            next_alive_turn(session)
        # 如果 AI 只是用了索要或看牌，没有结束回合，循环会继续，让 AI 再动一次

@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(BASE_DIR, filename)

@app.post("/api/games")
def create_game():
    seats = [Seat("player", "你", is_human=True)] + [Seat(f"bot-{index + 1}", name) for index, name in enumerate(BOT_NAMES)]
    session = Gamesession(game_id=uuid.uuid4().hex,seats=seats)
    session.deck = build_deck(lens(seats))

# 发初始手牌（每人4张普通牌 + 1张拆弹）
    for seat in session.seats:
        for _ in range(4):
            if session.deck: seat.hand.append(session.deck.pop(0))
        seat.hand.append("defuse") # 强行发一张拆弹
        # 从牌堆移除一张拆弹以平衡
        if "defuse" in session.deck: session.deck.remove("defuse")
        
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
    if not session or session.game_over: return fail("牌局不可用", 400)
    if not session.current_seat.is_human: return fail("不是你的回合", 409)
    if session.drawn_this_turn: return fail("本回合已抽过牌", 409)
    
    card = session.deck.pop(0) # 真正的抽牌
    session.drawn_this_turn = True
    exploded = False
    defused = False

    if card == "exploding_kitten":
        if "defuse" in session.human.hand:
            session.human.hand.remove("defuse")
            session.human.hand.append(card) # 获得炸弹猫（可用于后续打出或保留）
            defused = True
            add_log(session, "你抽到了炸弹猫！但你使用了【拆弹】。")
            
            insert_pos = random.randint(1, len(session.deck)) if session.deck else 0
            session.deck.insert(insert_pos, card)
        else:
            exploded = True
            session.human.alive = False
            session.game_over = True
            add_log(session, "你抽到了炸弹猫且没有拆弹，被淘汰！")
    else:
        session.human.hand.append(card)
        add_log(session, f"你抽取了 {CARD_LIBRARY[card]['name']}。")

    return jsonify({
        "ok": True, 
        "drawnCards": [card], 
        "exploded": exploded, 
        "defused": defused, 
        "game": session.public_state()
    })


@app.post("/api/games/<game_id>/play")
def play_card(game_id: str):
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    if session.game_over:
        return fail("本局游戏已结束。", 409)
        
    card_id = (request.get_json(silent=True) or {}).get("cardId")
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    if card_id not in CARD_LIBRARY or card_id not in session.human.hand:
        return fail("该卡牌不在你的手牌中。", 422)
        
    
    session.human.hand.remove(card_id)
    session.discard = card_id
    add_log(session, f"你打出 {CARD_LIBRARY[card_id]['name']}。")
    
    
    if card_id == "attack":
        session.extra_turns += 1
        add_log(session, "攻击生效：下一个对手将承受额外回合！")
    elif card_id == "skip":
        # 跳过牌：直接视为本回合已操作完毕，且不能抽牌
        session.drawn_this_turn = True
        add_log(session, "跳过生效：直接结束本回合。")
    elif card_id == "exploding_kitten":
        
        add_log(session, "你打出了炸弹猫！")

    return jsonify({"ok": True, "game": session.public_state()})


@app.post("/api/games/<game_id>/end-turn")
def end_turn(game_id: str):
    session = get_session(game_id)
    if not session:
        return fail("牌局不存在或已失效。", 404)
    if not session.current_seat.is_human:
        return fail("当前不是你的回合。", 409)
    session.drawn_this_turn = False
    run_until_human(session)
    return jsonify({"ok": True, "game": session.public_state()})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
