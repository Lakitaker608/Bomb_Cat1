const CARD_LIBRARY = {
  defuse: { name: "拆弹" },
  skip: { name: "跳过" },
  favor: { name: "索要" },
  see: { name: "预知未来" },
  shuffle: { name: "洗牌" },
  attack: { name: "攻击" },
  taco: { name: "墨西哥卷猫" },
  rainbow: { name: "彩虹猫" },
  potato: { name: "土豆猫" },
  braid_uncle: { name: "辫子大叔" },
  exploding_kitten: { name: "炸弹猫" }
};

const SAFE_DRAW_POOL = Object.keys(CARD_LIBRARY).filter((cardId) => cardId !== "exploding_kitten");
const BOT_NAMES = ["BombBot", "橘猫队长", "闪电爪", "夜行喵"];

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...headers }
  });
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const allowedOrigin = env.FRONTEND_ORIGIN || "";
  return {
    "Access-Control-Allow-Origin": origin && origin === allowedOrigin ? origin : allowedOrigin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin"
  };
}

function withCors(response, request, env) {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(corsHeaders(request, env))) {
    if (value) headers.set(key, value);
  }
  return new Response(response.body, { status: response.status, headers });
}

function error(message, status = 400) {
  return json({ ok: false, error: message }, status);
}

function createGame(gameId) {
  return {
    gameId,
    deck: 34,
    seats: [
      { id: "player", name: "你", isHuman: true, hand: [], alive: true },
      ...BOT_NAMES.map((name, index) => ({ id: `bot-${index + 1}`, name, isHuman: false, hand: [], alive: true }))
    ],
    discard: "attack",
    turnIndex: 0,
    openingDealt: [],
    playerDrewThisTurn: false,
    bombsRemaining: 4,
    gameOver: false,
    winnerId: null,
    logs: ["五人制对局开始：每位玩家先各抽取四张起始手牌。首轮结束后，炸弹猫将混入牌库。"]
  };
}

function stampLog(state, text) {
  const time = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" }).format(new Date());
  state.logs.push(`${time}|${text}`);
}

function currentSeat(state) {
  return state.seats[state.turnIndex];
}

function livingSeats(state) {
  return state.seats.filter((seat) => seat.alive);
}

function nextAliveTurn(state) {
  for (let offset = 0; offset < state.seats.length; offset += 1) {
    state.turnIndex = (state.turnIndex + 1) % state.seats.length;
    if (currentSeat(state).alive) return;
  }
}

function checkWinner(state) {
  const survivors = livingSeats(state);
  if (survivors.length === 1) {
    state.gameOver = true;
    state.winnerId = survivors[0].id;
    stampLog(state, `${survivors[0].name} 成为最后幸存者，赢得本局！`);
  }
}

function randomSafeCard() {
  return SAFE_DRAW_POOL[Math.floor(Math.random() * SAFE_DRAW_POOL.length)];
}

function drawOne(state, seat, { opening = false } = {}) {
  if (state.deck <= 0) return { kind: "empty" };

  const bombsEnabled = state.openingDealt.length === state.seats.length;
  const shouldExplode = !opening && bombsEnabled && state.bombsRemaining > 0 && Math.random() < state.bombsRemaining / state.deck;
  state.deck -= 1;

  if (shouldExplode) {
    state.bombsRemaining -= 1;
    state.discard = "exploding_kitten";
    const defuseIndex = seat.hand.indexOf("defuse");
    if (defuseIndex !== -1) {
      seat.hand.splice(defuseIndex, 1);
      state.discard = "defuse";
      stampLog(state, `${seat.name} 抽到炸弹猫，使用拆弹成功化解！`);
      return { kind: "defused", cardId: "defuse" };
    }
    seat.alive = false;
    seat.hand = [];
    stampLog(state, `${seat.name} 抽到炸弹猫且没有拆弹，被淘汰！`);
    checkWinner(state);
    return { kind: "exploded", cardId: "exploding_kitten" };
  }

  const cardId = randomSafeCard();
  seat.hand.push(cardId);
  return { kind: "card", cardId };
}

function publicSeat(seat) {
  return { id: seat.id, name: seat.name, cardCount: seat.hand.length, alive: seat.alive, isHuman: seat.isHuman };
}

function publicState(state) {
  const player = state.seats[0];
  const activeSeat = currentSeat(state);
  return {
    gameId: state.gameId,
    mode: "five-player",
    deck: state.deck,
    playerHand: player.hand,
    opponents: state.seats.slice(1).map(publicSeat),
    discard: state.discard,
    turn: activeSeat.isHuman ? "player" : "agent",
    currentPlayerId: activeSeat.id,
    currentPlayerName: activeSeat.name,
    openingRoundComplete: state.openingDealt.length === state.seats.length,
    canDraw: activeSeat.isHuman && player.alive && !state.playerDrewThisTurn && state.deck > 0 && !state.gameOver,
    drawCountNext: state.openingDealt.includes("player") ? 1 : 4,
    bombsRemaining: state.bombsRemaining,
    playerAlive: player.alive,
    gameOver: state.gameOver,
    winnerId: state.winnerId,
    logs: state.logs.slice(-30)
  };
}

function agentPayload(state, seat) {
  return {
    game: "bomb-cat",
    mode: "five-player",
    gameId: state.gameId,
    actor: publicSeat(seat),
    deckCount: state.deck,
    players: state.seats.map(publicSeat),
    discard: state.discard,
    allowedActions: ["draw", "play"],
    timestamp: new Date().toISOString()
  };
}

async function remoteAgentMove(state, seat, env) {
  if (!env.AGENT_ENDPOINT) return null;
  const headers = { "Content-Type": "application/json" };
  if (env.AGENT_TOKEN) headers.Authorization = `Bearer ${env.AGENT_TOKEN}`;
  try {
    const response = await fetch(env.AGENT_ENDPOINT, {
      method: "POST",
      headers,
      body: JSON.stringify(agentPayload(state, seat))
    });
    if (!response.ok) return null;
    const move = await response.json();
    return move && ["draw", "play"].includes(move.action) ? move : null;
  } catch {
    return null;
  }
}

async function applyBotTurn(state, seat, env) {
  if (!state.openingDealt.includes(seat.id)) {
    const count = Math.min(4, state.deck);
    for (let index = 0; index < count; index += 1) drawOne(state, seat, { opening: true });
    state.openingDealt.push(seat.id);
    stampLog(state, `${seat.name} 抽取了 ${count} 张起始手牌。`);
    return;
  }

  const move = await remoteAgentMove(state, seat, env) || (seat.hand.length && Math.random() > 0.68 ? { action: "play", cardId: "attack" } : { action: "draw" });
  if (move.action === "play" && seat.hand.includes(move.cardId) && move.cardId !== "exploding_kitten") {
    seat.hand.splice(seat.hand.indexOf(move.cardId), 1);
    state.discard = move.cardId;
    stampLog(state, `${seat.name} 打出 ${CARD_LIBRARY[move.cardId].name}。`);
    return;
  }

  const outcome = drawOne(state, seat);
  if (outcome.kind === "card") stampLog(state, `${seat.name} 摸了一张牌。`);
}

async function runUntilHuman(state, env) {
  nextAliveTurn(state);
  while (!currentSeat(state).isHuman && !state.gameOver) {
    await applyBotTurn(state, currentSeat(state), env);
    if (!state.gameOver) nextAliveTurn(state);
  }
}

export class GameRoom {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const method = request.method;
    let state = await this.ctx.storage.get("state");

    if (method === "POST" && url.pathname === "/internal/init") {
      if (!state) {
        const gameId = url.searchParams.get("gameId");
        if (!gameId) return error("缺少牌局编号。", 400);
        state = createGame(gameId);
        await this.ctx.storage.put("state", state);
      }
      return json({ ok: true, game: publicState(state) }, 201);
    }

    if (!state) return error("牌局不存在或已失效。", 404);
    if (method === "GET") return json({ ok: true, game: publicState(state) });

    if (method === "POST" && url.pathname.endsWith("/draw")) {
      const player = state.seats[0];
      if (!currentSeat(state).isHuman) return error("当前不是你的回合。", 409);
      if (state.gameOver || !player.alive) return error("本局已结束，无法继续摸牌。", 409);
      if (state.playerDrewThisTurn) return error("本回合已完成抽牌；请结束回合，等待其他玩家完成行动。", 409);
      const opening = !state.openingDealt.includes("player");
      const count = Math.min(opening ? 4 : 1, state.deck);
      if (!count) return error("牌库已空，本局结束。", 409);

      const outcomes = Array.from({ length: count }, () => drawOne(state, player, { opening }));
      state.playerDrewThisTurn = true;
      if (opening) {
        state.openingDealt.push("player");
        stampLog(state, `你抽取了 ${count} 张起始手牌。`);
      }
      const exploded = outcomes.some((outcome) => outcome.kind === "exploded");
      const defused = outcomes.some((outcome) => outcome.kind === "defused");
      if (!player.alive && !state.gameOver) {
        state.gameOver = true;
        stampLog(state, "你已被淘汰，本局对你结束。");
      }
      await this.ctx.storage.put("state", state);
      return json({ ok: true, drawnCards: outcomes.filter((outcome) => outcome.kind === "card").map((outcome) => outcome.cardId), exploded, defused, game: publicState(state) });
    }

    if (method === "POST" && url.pathname.endsWith("/play")) {
      const player = state.seats[0];
      if (!currentSeat(state).isHuman || state.gameOver) return error("当前无法出牌。", 409);
      const { cardId } = await request.json().catch(() => ({}));
      if (!CARD_LIBRARY[cardId] || cardId === "exploding_kitten" || !player.hand.includes(cardId)) return error("该卡牌不在你的手牌中。", 422);
      player.hand.splice(player.hand.indexOf(cardId), 1);
      state.discard = cardId;
      stampLog(state, `你打出 ${CARD_LIBRARY[cardId].name}。`);
      await this.ctx.storage.put("state", state);
      return json({ ok: true, game: publicState(state) });
    }

    if (method === "POST" && url.pathname.endsWith("/end-turn")) {
      const player = state.seats[0];
      if (!currentSeat(state).isHuman || state.gameOver || !player.alive) return error("当前无法结束回合。", 409);
      if (!player.hand.length) return error("请先抽取四张起始手牌。", 422);
      await runUntilHuman(state, this.env);
      state.playerDrewThisTurn = false;
      await this.ctx.storage.put("state", state);
      return json({ ok: true, game: publicState(state) });
    }

    return error("未找到接口。", 404);
  }
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders(request, env) });
    const url = new URL(request.url);
    const createMatch = request.method === "POST" && url.pathname === "/api/games";
    const gameMatch = url.pathname.match(/^\/api\/games\/([a-zA-Z0-9-]+)(?:\/(draw|play|end-turn))?$/);

    if (!createMatch && !gameMatch) return withCors(error("未找到接口。", 404), request, env);

    const gameId = createMatch ? crypto.randomUUID() : gameMatch[1];
    const objectId = env.GAME_ROOM.idFromName(gameId);
    const room = env.GAME_ROOM.get(objectId);

    if (createMatch) {
      const response = await room.fetch(new Request(`https://game-room/internal/init?gameId=${encodeURIComponent(gameId)}`, { method: "POST" }));
      return withCors(new Response(response.body, { status: 201, headers: response.headers }), request, env);
    }

    const suffix = gameMatch[2] ? `/${gameMatch[2]}` : "/";
    const forwarded = new Request(`https://game-room/api/games/${gameId}${suffix}`, request);
    return withCors(await room.fetch(forwarded), request, env);
  }
};
