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
    extra_turns: int = 0
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
        bombs = sum(1 for c in self.deck if c == "exploding_kitten")
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

def build_deck(player_count: int) -> list[str]:
    deck = []
    for _ in range(6): deck.extend(["skip", "attack", "see", "shuffle"])
    for _ in range(3): deck.extend(["favor"])
    for _ in range(4): deck.extend(["taco", "rainbow", "potato", "braid_uncle"])
    
    bombs = player_count - 1
    defuse_cards = player_count + 1
    
    # Fisher-Yates 洗牌
    for i in range(len(deck) - 1, 0, -1):
        j = random.randint(0, i)
        deck[i], deck[j] = deck[j], deck[i]
    
    # 均匀散布炸弹
    segment_size = len(deck) // bombs
    for i in range(bombs):
        insert_pos = random.randint(i * segment_size, (i + 1) * segment_size - 1)
        deck.insert(insert_pos, "exploding_kitten")
        
    deck.extend(["defuse"] * defuse_cards)
    
    for i in range(len(deck) - 1, 0, -1):
        j = random.randint(0, i)
        deck[i], deck[j] = deck[j], deck[i]
        
    return deck
    
def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status

def get_session(game_id: str) -> GameSession | None:
    with SESSIONS_LOCK:
        return SESSIONS.get(game_id)

def add_log(session: GameSession, text: str) -> None:
    session.logs.append(f"{datetime.now().strftime('%H:%M')}|{text}")


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
    # 检查当前玩家是否还有未完成的“额外回合”
    if session.current_seat.extra_turns > 0:
        session.current_seat.extra_turns -= 1
        session.drawn_this_turn = False
        return
        
    # 正常轮转下一个存活玩家
    for _ in range(len(session.seats)):
        session.turn_index = (session.turn_index + 1) % len(session.seats)
        if session.current_seat.alive:
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
        # 简单的 AI 策略
        if "attack" in seat.hand and random.random() > 0.6:
            move = {"action": "play", "cardId": "attack"}
        elif "shuffle" in seat.hand and random.random() > 0.7:
            move = {"action": "play", "cardId": "shuffle"}
        elif "favor" in seat.hand and random.random() > 0.7:
            move = {"action": "play", "cardId": "favor"}
        elif "skip" in seat.hand and random.random() > 0.8:
            move = {"action": "play", "cardId": "skip"}
        else:
            move = {"action": "draw"}

    card_id = move.get("cardId")

    if move.get("action") == "play" and card_id in seat.hand and card_id in CARD_LIBRARY:
        seat.hand.remove(card_id)
        session.discard = card_id
        add_log(session, f"{seat.name} 打出 {CARD_LIBRARY[card_id]['name']}。")
        if card_id == "attack":
            # 寻找下一个存活玩家
            target_seat = None
            for i in range(1, len(session.seats)):
                target_idx = (session.turn_index + i) % len(session.seats)
                if session.seats[target_idx].alive:
                    target_seat = session.seats[target_idx]
                    break
                    
            if target_seat:
                target_seat.extra_turns += 1
                add_log(session, f"{seat.name} 攻击生效：{target_seat.name} 将承受额外回合！")
        elif card_id == "skip":
            add_log(session, f"{seat.name} 使用了跳过，结束回合。")
            return 
        elif card_id == "shuffle":
            random.shuffle(session.deck)
            add_log(session, f"{seat.name} 使用了洗牌，牌堆顺序被重置。")
        elif card_id == "see":
            add_log(session, f"{seat.name} 使用了预知未来。")
        elif card_id == "favor":
            target_seat = None
            for i in range(1, len(session.seats)):
                target_idx = (session.turn_index + i) % len(session.seats)
                if session.seats[target_idx].alive:
                    target_seat = session.seats[target_idx]
                    break
            if target_seat and target_seat.hand:
                stolen_card = random.choice(target_seat.hand)
                target_seat.hand.remove(stolen_card)
                seat.hand.append(stolen_card)
                add_log(session, f"{seat.name} 向 {target_seat.name} 索要了一张牌。")
            else:
                add_log(session, f"{seat.name} 使用了【索要】，但对方没有手牌。")
            
    elif session.deck:
        card = session.deck.pop(0)
        if card == "exploding_kitten":
            if "defuse" in seat.hand:
                seat.hand.remove("defuse")
                add_log(session, f"{seat.name} 抽到了炸弹猫，但使用了【拆弹】成功化解。")
                insert_pos = random.randint(1, len(session.deck)) if len(session.deck) > 1 else 0
                session.deck.insert(insert_pos, card)
            else:
                seat.alive = False
                add_log(session, f"💥 {seat.name} 抽到了炸弹猫且没有拆弹，被炸死淘汰！")
                session.deck.extend(seat.hand)
                seat.hand.clear()
                random.shuffle(session.deck)
                check_win_condition(session)
        else:
            seat.hand.append(card)
            add_log(session, f"{seat.name} 摸了一张牌。")
    else:
        add_log(session, f"牌堆已空，{seat.name} 无法摸牌。")

def run_until_human(session: GameSession) -> None:
    next_alive_turn(session)
    while not session.current_seat.is_human and sum(1 for s in session.seats if s.alive) > 1:
        apply_bot_turn(session, session.current_seat)
        # (如果是攻击导致的额外回合，next_alive_turn 内部会根据 extra_turns 处理)
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

    # 1. 先生成一份纯普通牌的牌库（绝对不含炸弹）
    deck = []
    for _ in range(6): deck.extend(["skip", "attack", "see", "shuffle"])
    for _ in range(3): deck.extend(["favor"])
    for _ in range(4): deck.extend(["taco", "rainbow", "potato", "braid_uncle"])
    
    # 彻底打乱普通牌
    random.shuffle(deck)

    # 2. 开始发牌：给每人发 4 张普通牌
    for seat in session.seats:
        for _ in range(4):
            if deck: 
                seat.hand.append(deck.pop(0))
                
    # 3. 给每人强行发 1 张拆弹（放入手牌，防止起手没拆弹直接死）
    for seat in session.seats:
        seat.hand.append("defuse")

    # 4. 往剩余牌库里补充多余的拆弹（5人局总共发5张，额外留1张在牌库）
    extra_defuse_count = 1
    deck.extend(["defuse"] * extra_defuse_count)

    # 5. 现在手牌发完了，开始把炸弹塞进剩余牌库
    bombs = len(session.seats) - 1 # 5人局塞4个炸弹
    if bombs > 0 and len(deck) > 0:
        segment_size = len(deck) // bombs
        for i in range(bombs):
            # 随机选择在当前区段插入
            min_pos = i * segment_size
            max_pos = (i + 1) * segment_size - 1
            if min_pos > len(deck): min_pos = len(deck)
            if max_pos > len(deck): max_pos = len(deck)
            
            insert_pos = random.randint(min_pos, max_pos) if min_pos <= max_pos else len(deck)
            deck.insert(insert_pos, "exploding_kitten")

    # 6. 最终洗牌，确保炸弹位置绝对随机
    random.shuffle(deck)
    session.deck = deck

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
    if not session.deck: return fail("牌库已空", 409)
    
    card = session.deck.pop(0) 
    session.drawn_this_turn = True
    exploded = False
    need_reinsert = False
    defused = False

    if card == "exploding_kitten":
        if "defuse" in session.human.hand:
            session.human.hand.remove("defuse")
            defused = True
            need_reinsert = True
            add_log(session, "你抽到了炸弹猫！使用了【拆弹】，请选择炸弹放回的位置。")
        else:
            exploded = True
            session.human.alive = False
            session.game_over = True
            add_log(session, "你抽到了炸弹猫且没有拆弹，被淘汰！")
    else:
        session.human.hand.append(card)
        add_log(session, f"你抽取了 {CARD_LIBRARY[card]['name']}。")
        run_until_human(session)

    return jsonify({
        "ok": True, 
        "drawnCards": [card], 
        "exploded": exploded, 
        "defused": defused, 
        "need_reinsert": need_reinsert, # 告诉前端是否要弹窗选位置
        "game": session.public_state()
    })

@app.post("/api/games/<game_id>/reinsert-bomb")
def reinsert_bomb(game_id: str):
    session = get_session(game_id)
    if not session: return fail("牌局不存在", 404)
    
    data = request.get_json(silent=True) or {}
    index = data.get("index", len(session.deck))
    
    index = max(0, min(index, len(session.deck)))
    
    session.deck.insert(index, "exploding_kitten")
    add_log(session, f"你将炸弹猫放回了牌堆。")
    
    run_until_human(session)
    
    return jsonify({"ok": True, "game": session.public_state()})
    
@app.post("/api/games/<game_id>/play")
def play_card(game_id: str):
    session = get_session(game_id)
    if not session: return fail("牌局不存在或已失效。", 404)
    if session.game_over: return fail("本局游戏已结束。", 409)
        
    card_id = (request.get_json(silent=True) or {}).get("cardId")
    if not session.current_seat.is_human: return fail("当前不是你的回合。", 409)
    if card_id not in CARD_LIBRARY or card_id not in session.human.hand: return fail("该卡牌不在你的手牌中。", 422)
        
    session.human.hand.remove(card_id)
    session.discard = card_id
    add_log(session, f"你打出 {CARD_LIBRARY[card_id]['name']}。")
    
    peek_cards = [] # 用于存储预知未来的牌

    if card_id == "attack":
        target_seat = None
        for i in range(1, len(session.seats)):
            target_idx = (session.turn_index + i) % len(session.seats)
            if session.seats[target_idx].alive:
                target_seat = session.seats[target_idx]
                break
        if target_seat:
            target_seat.extra_turns += 1
            # 【修复】把 seat.name 改成 你
            add_log(session, f"你攻击生效：{target_seat.name} 将承受额外回合！")
            
    elif card_id == "skip":
        session.drawn_this_turn = True
        add_log(session, "跳过生效：直接结束本回合。")
        # 【新增】打出跳过牌后，直接结束你的回合，让 AI 行动
        run_until_human(session)
        
    elif card_id == "shuffle":
        random.shuffle(session.deck)
        add_log(session, "你使用了洗牌，牌堆顺序已被彻底重置！")
        
    elif card_id == "see":
        peek_cards = session.deck[:3] # 获取顶部三张牌，不改变牌堆
        names = "、".join(CARD_LIBRARY[c]["name"] for c in peek_cards)
        add_log(session, f"你看到了牌堆顶部：{names}。")
        
    elif card_id == "favor":
        # 寻找下一个存活的玩家作为目标
        target_seat = None
        for i in range(1, len(session.seats)):
            target_idx = (session.turn_index + i) % len(session.seats)
            if session.seats[target_idx].alive:
                target_seat = session.seats[target_idx]
                break
        
        if target_seat:
            if target_seat.hand:
                stolen_card = random.choice(target_seat.hand)
                target_seat.hand.remove(stolen_card)
                session.human.hand.append(stolen_card)
                add_log(session, f"你向 {target_seat.name} 索要了一张牌，获得了【{CARD_LIBRARY[stolen_card]['name']}】。")
            else:
                add_log(session, f"你向 {target_seat.name} 索要，但对方没有手牌。")

    return jsonify({
        "ok": True, 
        "game": session.public_state(),
        "peek": peek_cards # 将看到的牌发给前端
    })

@app.post("/api/games/<game_id>/end-turn-no-draw")
def end_turn_no_draw(game_id: str):
    session = get_session(game_id)
    if not session or session.game_over: return fail("牌局不可用", 400)
    if not session.current_seat.is_human: return fail("不是你的回合", 409)
    
    session.drawn_this_turn = True
    add_log(session, "你选择了不抽牌，直接结束回合。")
    
    run_until_human(session)
    
    return jsonify({"ok": True, "game": session.public_state()})
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
