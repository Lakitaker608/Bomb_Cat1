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
    discard: str = "attack"
    turn_index: int = 0
    drawn_this_turn: bool = False
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
            "bombsRemaining": sum(1 for c in self.deck if c == "exploding_kitten"),
            "gameOver": not self.human.alive or alive_count <= 1,
            "playerAlive": self.human.alive,
            "winnerId": "player" if self.human.alive and alive_count == 1 else None,
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
    current = session.current_seat
    
    # 如果当前玩家身上还有被攻击积攒的额外回合
    if current.extra_turns > 0:
        current.extra_turns -= 1
        session.drawn_this_turn = False
        add_log(session, f"{current.name} 受到攻击，必须再进行一个回合！")
        return
    
    # 正常切换到下一个存活玩家
    for _ in range(len(session.seats)):
        session.turn_index = (session.turn_index + 1) % len(session.seats)
        if session.current_seat.alive:
            session.drawn_this_turn = False
            return
            
def apply_bot_turn(session: GameSession, seat: Seat) -> None:
    if seat.hand and random.random() > 0.6:
        playable = [c for c in seat.hand if in ("attack","skip","favor","see","shuffle")]
        if playable:
            card_id == random.choice(playable)
            seat.hand.remove(card_id)
            session_discard = card_id
            if card_id == "attack":
                    carry_over = seat.extra_turns
                    seat.extra_turns = 0
                    
                    target_index = (session.turn_index + 1) % len(session.seats)
                    while not session.seats[target_index].alive:
                        target_index = (target_index + 1) % len(session.seats)
                    target = session.seats[target_index]
                    target.extra_turns += (1 + carry_over)
                    
                    session.drawn_this_turn = True
                    add_log(session, f"{seat.name} 打出了 攻击！{target.name} 必须连续行动 {2+carry_over} 回合。")
                    return
                    
                elif card_id == "skip":
                    seat.extra_turns = 0
                    session.drawn_this_turn = True
                    add_log(session, f"{seat.name} 打出了 跳过，结束回合。")
                    return
                elif card_id == "favor":
                    victims = [s for s in session.seats if s.alive and s.seat_id != seat.seat_id and s.hand]
                    if victims:
                        victim = random.choice(victims)
                        stolen = victim.hand.pop(random.randint(0, len(victim.hand)-1))
                        seat.hand.append(stolen)
                        add_log(session, f"{seat.name} 对 {victim.name} 使用了 索要，抢走了一张牌。")
                elif card_id == "see":
                    add_log(session, f"{seat.name} 使用了 预知未来。")
                elif card_id == "shuffle":
                    random.shuffle(session.deck)
                    add_log(session, f"{seat.name} 洗了牌库。")
                return # 出牌后结束当前函数，在下一次 while 循环中继续行动
    
        # 摸牌
        if session.deck:
            drawn = session.deck.pop(0)
            if drawn == "exploding_kitten":
                if "defuse" in seat.hand:
                    seat.hand.remove("defuse")
                    session.deck.insert(random.randint(0, len(session.deck)), drawn)
                    add_log(session, f"{seat.name} 抽到炸弹猫，使用拆弹卡化解。")
                else:
                    seat.alive = False
                    add_log(session, f"{seat.name} 抽到炸弹猫，被淘汰了！")
            else:
                seat.hand.append(drawn)
                add_log(session, f"{seat.name} 摸了一张牌。")
            session.drawn_this_turn = True

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
    
    # 1. 初始化基础牌库（不含炸弹）
    base_deck = []
    for _ in range(24): # 普通牌
        base_deck.append(random.choice(NORMAL_DRAW_POOL))
    for _ in range(15): # 拆弹卡
        base_deck.append("defuse")
    random.shuffle(base_deck)
    
    # 2. 开局发牌：每人发 4 张手牌 + 1 张拆弹卡
    for seat in seats:
        seat.hand.append("defuse")
        for _ in range(4):
            if base_deck:
                seat.hand.append(base_deck.pop())
                
    # 3. 根据规则（人数 - 1），将炸弹猫洗入牌库
    for _ in range(len(seats) - 1):
        base_deck.append("exploding_kitten")
    random.shuffle(base_deck)
    
    session = GameSession(game_id=uuid.uuid4().hex, seats=seats, deck=base_deck)
    session.logs.append("游戏开始，已为每人发放 4 张手牌和 1 张拆弹卡。")
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
        return fail("本回合已经抽过牌了，请结束回合。", 409)
    if not session.deck:
        return fail("牌库已空，本局结束。", 409)
        
    card_id = session.deck.pop(0)
    session.drawn_this_turn = True
    exploded = False
    defused = False
    
    if card_id == "exploding_kitten":
        exploded = True
        if "defuse" in session.human.hand:
            defused = True
            session.human.hand.remove("defuse")
            # 简单化：随机插回牌堆
            session.deck.insert(random.randint(0, len(session.deck)), card_id)
            add_log(session, f"你抽到了炸弹猫！使用拆弹卡化解，并将其秘密放回牌库。")
        else:
            session.human.alive = False
            add_log(session, f"你抽到了炸弹猫，没有拆弹卡，被淘汰了！")
    else:
        session.human.hand.append(card_id)
        add_log(session, f"你抽到了 {CARD_LIBRARY[card_id]['name']}。")
        
    if not session.human.alive:
        run_until_human(session)
        
    return jsonify({
        "ok": True, 
        "drawnCards": [card_id] if card_id != "exploding_kitten" else [],
        "exploded": exploded,
        "defused": defused,
        "game": session.public_state()
    })

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
    extra_data = {}
    log_msg = f"你打出 {CARD_LIBRARY[card_id]['name']}。"

    if card_id == "skip":
        session.drawn_this_turn = True # 强行结束回合，不能再抽牌
        session.human.extra_turns = 0   # 跳过牌会直接作废自己身上的攻击惩罚
        log_msg += " 本回合结束，无需抽牌。"
        
    elif card_id == "attack":
        session.drawn_this_turn = True # 强行结束回合
        carry_over = session.human.extra_turns # 把当前自己受到的攻击也转移出去
        session.human.extra_turns = 0
        
        # 找到下一个存活的玩家
        target_index = (session.turn_index + 1) % len(session.seats)
        while not session.seats[target_index].alive:
            target_index = (target_index + 1) % len(session.seats)
        target = session.seats[target_index]
        
        # 被攻击者获得 1个基础额外回合 + 转移过来的惩罚
        target.extra_turns += (1 + carry_over)
        log_msg += f" {target.name} 必须连续行动 {2 + carry_over} 回合！"
        
    elif card_id == "shuffle":
        random.shuffle(session.deck)
        log_msg += " 牌库已被重新洗匀。"
        
    elif card_id == "see":
        peeked = session.deck[:3]
        extra_data["peeked"] = [CARD_LIBRARY[c]["name"] for c in peeked]
        log_msg += f" 你看到了接下来的牌：{', '.join(extra_data['peeked'])}。"

    elif card_id == "favor":
        victims = [s for s in session.seats if s.alive and not s.is_human and s.hand]
        if victims:
            victim = random.choice(victims)
            stolen = victim.hand.pop(random.randint(0, len(victim.hand)-1))
            session.human.hand.append(stolen)
            log_msg += f" 从 {victim.name} 手中索要到了 {CARD_LIBRARY[stolen]['name']}。"
        else:
            log_msg += " 但其他玩家没有手牌。"

    # 猫咪卡连击
    elif card_id in ("taco", "rainbow", "potato", "braid_uncle"):
        if card_id in session.human.hand:
            session.human.hand.remove(card_id)
            victims = [s for s in session.seats if s.alive and not s.is_human and s.hand]
            if victims:
                victim = random.choice(victims)
                stolen = victim.hand.pop(random.randint(0, len(victim.hand)-1))
                session.human.hand.append(stolen)
                log_msg += f" 触发双猫连击！从 {victim.name} 手中随机抢走了一张牌。"
            else:
                log_msg += " 但其他玩家没有手牌可以抢。"
        else:
            log_msg += " （单张猫咪牌没有效果）。"

    add_log(session, log_msg)
    return jsonify({"ok": True, "extra": extra_data, "game": session.public_state()})


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
