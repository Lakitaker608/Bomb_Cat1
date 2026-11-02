/* Presentation layer only. The existing game API remains the source of truth. */
const CARD_LIBRARY = {
  defuse: { name: "拆弹", english: "Defuse", icon: "shield", className: "type-defuse", description: "抵消炸弹猫", category: "防御牌" },
  skip: { name: "跳过", english: "Skip", icon: "skip", className: "type-skip", description: "结束本回合", category: "行动牌" },
  favor: { name: "索要", english: "Favor", icon: "gift", className: "type-favor", description: "向对手索要一张牌", category: "行动牌" },
  see: { name: "预知未来", english: "See Future", icon: "eye", className: "type-see", description: "查看牌库顶三张", category: "行动牌" },
  shuffle: { name: "洗牌", english: "Shuffle", icon: "shuffle", className: "type-shuffle", description: "重置牌库顺序", category: "行动牌" },
  attack: { name: "攻击", english: "Attack", icon: "bolt", className: "type-attack", description: "迫使对手连续行动", category: "行动牌" },
  taco: { name: "墨西哥卷猫", english: "Taco Cat", icon: "cat", className: "type-taco", description: "普通猫牌 · 组合效果以服务端规则为准", category: "普通猫牌" },
  rainbow: { name: "彩虹猫", english: "Rainbow Cat", icon: "cat", className: "type-rainbow", description: "普通猫牌 · 组合效果以服务端规则为准", category: "普通猫牌" },
  potato: { name: "土豆猫", english: "Potato Cat", icon: "cat", className: "type-potato", description: "普通猫牌 · 组合效果以服务端规则为准", category: "普通猫牌" },
  braid_uncle: { name: "辫子大叔", english: "Braid Uncle", icon: "cat", className: "type-braid", description: "普通猫牌 · 组合效果以服务端规则为准", category: "普通猫牌" },
  exploding_kitten: { name: "炸弹猫", english: "Exploding Cat", icon: "bomb", className: "type-exploding", description: "抽到后需要拆弹，否则被淘汰", category: "危险牌" }
};
const UNKNOWN_CARD = { name: "未知卡牌", english: "Unknown", icon: "cards", className: "type-unknown", description: "当前客户端暂不支持此卡牌", category: "卡牌" };
const DEFAULT_OPPONENTS = ["BombBot", "橘猫队长", "闪电爪", "夜行喵"];
const game = {
  gameId: null, deck: null, opponents: [], hand: [], discarded: null, turn: "waiting",
  currentPlayerName: "你", currentPlayerId: null, selectedCard: null, selectedIndex: null,
  logs: [], busy: false, connected: false, recoveryRequired: false, gameOver: false, playerAlive: true,
  winnerId: null, canDraw: false, drawCountNext: 4, drewThisTurn: false, bombsRemaining: null
};
const $ = (selector) => document.querySelector(selector);
const handElement = $("#hand");
const statusText = $("#statusText");
const opponentSeats = $("#opponentSeats");
const logList = $("#logList");
let discardCard = $("#discardCard");
let hiddenLogPrefix = [];
let previouslyFocused = null;

async function api(path, options = {}) {
  const base = String(window.BOMB_CAT_API_BASE || "").replace(/\/+$/, "");
  const response = await fetch(`${base}/api${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    signal: AbortSignal.timeout(65000)
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) {
    const error = new Error(payload.error || "游戏服务没有返回有效结果，请确认后端已启动。");
    error.confirmedRejection = response.status < 500 && payload.ok === false;
    throw error;
  }
  return payload;
}

function setStatus(message, isError = false) {
  statusText.textContent = message;
  $(".status-banner").classList.toggle("is-error", isError);
}

function hydrateGame(state) {
  game.gameId = state.gameId;
  game.connected = true;
  game.deck = state.deck;
  game.hand = Array.isArray(state.playerHand) ? state.playerHand : [];
  game.opponents = Array.isArray(state.opponents) ? state.opponents : [];
  game.discarded = state.discard;
  game.turn = state.turn;
  game.currentPlayerName = state.currentPlayerName || "你";
  game.currentPlayerId = state.currentPlayerId;
  game.logs = Array.isArray(state.logs) ? state.logs : [];
  game.gameOver = state.gameOver === true;
  game.playerAlive = state.playerAlive !== false;
  game.winnerId = state.winnerId || null;
  game.canDraw = typeof state.canDraw === "boolean" ? state.canDraw : !game.drewThisTurn;
  // Older backends always draw four; newer ones return the exact next draw size.
  game.drawCountNext = state.drawCountNext ?? 4;
  game.bombsRemaining = state.bombsRemaining ?? null;
  game.selectedCard = null;
  game.selectedIndex = null;
  render();
}

function render() {
  $("#deckCount").textContent = game.deck ?? "—";
  $("#playerCards").textContent = game.hand.length;
  $("#connectionBadge").className = `connection-badge${game.connected ? " is-connected" : ""}`;
  $("#connectionBadge span").textContent = game.connected ? "牌局已连接" : "正在连接";
  const alive = game.opponents.filter((opponent) => opponent.alive !== false).length + Number(game.playerAlive);
  $("#aliveCount").textContent = game.connected ? `${alive} / 5 位玩家在场` : "等待牌局连接";
  $("#bombInfo").hidden = game.bombsRemaining === null;
  $("#bombInfo").textContent = game.bombsRemaining === null ? "" : `剩余炸弹 ${game.bombsRemaining} 张`;
  renderOpponents();
  updateDiscard(game.discarded);
  renderHand();
  renderLog();
  syncControls();
}

function populateCard(node, cardId) {
  const card = CARD_LIBRARY[cardId] || UNKNOWN_CARD;
  node.classList.add(card.className);
  node.classList.toggle("is-cat", card.icon === "cat");
  node.dataset.cardId = cardId;
  node.title = `${card.name}：${card.description}`;
  node.querySelector(".card-category").textContent = card.category;
  node.querySelector(".card-art-icon use").setAttribute("href", `#icon-${card.icon}`);
  node.querySelector(".card-mini use").setAttribute("href", `#icon-${card.icon === "cat" ? "paw" : card.icon}`);
  node.querySelector(".card-name").textContent = card.name;
  node.querySelector(".card-english").textContent = card.english;
}

function renderHand() {
  handElement.replaceChildren();
  if (!game.hand.length) {
    const empty = document.createElement("div");
    empty.className = "empty-hand";
    empty.innerHTML = '<svg aria-hidden="true"><use href="#icon-cards" /></svg><b></b><small></small>';
    empty.querySelector("b").textContent = game.gameOver ? "本局已结束" : "你的手牌区，正在待命";
    empty.querySelector("small").textContent = game.gameOver ? "可在对局动态中回顾本局" : game.connected ? "点击抽牌堆，领取你的手牌" : "连接牌局后，从抽牌堆领取起始手牌";
    handElement.appendChild(empty);
  } else {
    game.hand.forEach((cardId, index) => {
      const node = $("#cardTemplate").content.firstElementChild.cloneNode(true);
      populateCard(node, cardId);
      node.setAttribute("aria-label", `${CARD_LIBRARY[cardId]?.name || "未知卡牌"}，点击选择后确认出牌`);
      node.addEventListener("click", () => selectCard(cardId, index));
      handElement.appendChild(node);
    });
  }
  $(".hand-caption").textContent = game.gameOver ? "本局结束 · 手牌按服务端状态展示" : game.hand.length ? "点击选牌，再点击下方按钮确认" : "抽取手牌，开始这场猫咪对决";
}

function renderOpponents() {
  [...opponentSeats.children].forEach((node, index) => {
    const opponent = game.opponents[index];
    const active = !!opponent && !game.gameOver && (game.currentPlayerId ? opponent.id === game.currentPlayerId : opponent.name === game.currentPlayerName);
    const eliminated = opponent?.alive === false;
    node.classList.toggle("is-active", active);
    node.classList.toggle("is-eliminated", eliminated);
    node.classList.toggle("is-waiting", !opponent);
    node.querySelector("strong").textContent = opponent?.name || DEFAULT_OPPONENTS[index];
    node.querySelector("small").textContent = !opponent ? "等待连接" : eliminated ? "已淘汰" : game.gameOver ? "本局结束" : active ? "正在行动" : "等待回合";
    node.querySelector(".seat-count b").textContent = opponent?.cardCount ?? "—";
    node.setAttribute("aria-label", `${opponent?.name || DEFAULT_OPPONENTS[index]}，${node.querySelector("small").textContent}，手牌 ${opponent?.cardCount ?? "未知"} 张`);
    if (active) node.setAttribute("aria-current", "true"); else node.removeAttribute("aria-current");
  });
}

function renderLog() {
  logList.replaceChildren();
  // Hide only the entries the user cleared; newly received entries remain visible.
  let start = 0;
  for (let count = Math.min(hiddenLogPrefix.length, game.logs.length); count > 0; count -= 1) {
    if (hiddenLogPrefix.slice(-count).every((entry, index) => entry === game.logs[index])) { start = count; break; }
  }
  const entries = game.logs.slice(start).reverse();
  entries.forEach((entry) => {
    const text = String(entry);
    const separator = text.indexOf("|");
    const li = document.createElement("li");
    const time = document.createElement("time");
    const body = document.createElement("span");
    time.textContent = separator >= 0 ? text.slice(0, separator) : "开局";
    body.textContent = separator >= 0 ? text.slice(separator + 1) : text;
    li.append(time, body);
    logList.appendChild(li);
  });
  if (!entries.length) {
    const empty = document.createElement("li");
    empty.className = "log-empty";
    empty.textContent = game.connected ? "暂无新动态，下一步行动将记录在这里。" : "连接成功后，这里将显示真实的对局记录。";
    logList.appendChild(empty);
  }
}

function selectCard(cardId, index) {
  if (!canAct() || !CARD_LIBRARY[cardId] || cardId === "exploding_kitten") return;
  const deselect = game.selectedIndex === index;
  game.selectedCard = deselect ? null : cardId;
  game.selectedIndex = deselect ? null : index;
  [...handElement.querySelectorAll(".hand-card")].forEach((node, nodeIndex) => {
    const selected = nodeIndex === game.selectedIndex;
    node.classList.toggle("selected", selected);
    node.setAttribute("aria-pressed", String(selected));
    node.querySelector(".card-bottom").textContent = selected ? "已选择 · 再次点击取消" : "点击选择";
  });
  syncControls();
  setStatus(deselect ? "已取消选择。你可以重新选牌，或直接结束回合。" : `已选中「${CARD_LIBRARY[cardId].name}」，点击“出牌并结束”确认。`);
}

function updateDiscard(cardId) {
  if (!cardId) {
    discardCard.className = "discard empty-discard";
    discardCard.innerHTML = '<svg aria-hidden="true"><use href="#icon-cards" /></svg><b>暂无弃牌</b><small>打出的卡牌会留在这里</small>';
    discardCard.setAttribute("aria-label", "暂无弃牌");
    $("#discardName").textContent = "等待第一张弃牌";
    $("#discardDescription").textContent = "查看最近一张卡牌";
    return;
  }
  const face = $("#cardTemplate").content.firstElementChild.cloneNode(true);
  const next = document.createElement("div");
  next.id = "discardCard";
  next.className = "discard playing-card";
  next.append(...face.childNodes);
  populateCard(next, cardId);
  next.querySelector(".card-bottom").textContent = "DISCARD / 最新牌面";
  next.querySelector(".selection-mark").remove();
  const card = CARD_LIBRARY[cardId] || UNKNOWN_CARD;
  next.setAttribute("aria-label", `弃牌堆顶部：${card.name}`);
  discardCard.replaceWith(next);
  discardCard = next;
  $("#discardName").textContent = card.name;
  $("#discardDescription").textContent = card.description;
}

function canAct() {
  return game.connected && !!game.gameId && !game.busy && !game.recoveryRequired && game.turn === "player" && !game.gameOver && game.playerAlive;
}

function syncControls() {
  const active = canAct();
  const drawAllowed = active && game.canDraw && !game.drewThisTurn && game.deck > 0;
  $("#drawButton").disabled = !drawAllowed;
  $("#deckButton").disabled = !drawAllowed;
  $("#endButton").disabled = !active || game.hand.length === 0;
  $("#drawButton span").textContent = game.drawCountNext === 4 ? "抽取四张" : `抽取 ${game.drawCountNext} 张`;
  $(".deck-label").textContent = `抽取 ${game.drawCountNext} 张`;
  $("#deckButton").setAttribute("aria-label", `从牌库抽取 ${game.drawCountNext} 张牌`);
  $("#endButton span").textContent = game.selectedCard ? "出牌并结束" : "结束回合";
  handElement.querySelectorAll(".hand-card").forEach((node) => { node.disabled = !active || !CARD_LIBRARY[node.dataset.cardId] || node.dataset.cardId === "exploding_kitten"; });
  $(".player-zone").setAttribute("aria-busy", String(game.busy));
  if (!game.connected) {
    $("#turnPill").textContent = "等待连接";
    $("#actionTitle").textContent = "正在连接牌局";
    $("#actionHint").textContent = "连接成功后，四名 AI 对手将入座。";
  } else if (game.recoveryRequired) {
    $("#turnPill").textContent = "状态待确认";
    $("#actionTitle").textContent = "已暂停操作，避免重复出牌";
    $("#actionHint").textContent = "无法确认上次操作结果；刷新页面将创建新牌局。";
  } else if (game.gameOver || !game.playerAlive) {
    $("#turnPill").textContent = "本局结束";
    $("#actionTitle").textContent = game.winnerId === "player" ? "你是最后的幸存者" : "本局已结束";
    $("#actionHint").textContent = game.playerAlive ? "可在对局动态中回顾本局。" : "你已被淘汰，无法继续出牌或抽牌。";
  } else if (game.busy || game.turn !== "player") {
    $("#turnPill").textContent = game.busy ? "处理中" : `${game.currentPlayerName} 的回合`;
    $("#actionTitle").textContent = game.busy ? "正在处理行动" : `${game.currentPlayerName} 正在行动`;
    $("#actionHint").textContent = "请等待服务端返回结果，不要重复操作。";
  } else if (game.selectedCard) {
    $("#turnPill").textContent = "你的回合";
    $("#actionTitle").textContent = `准备使用：${CARD_LIBRARY[game.selectedCard].name}`;
    $("#actionHint").textContent = CARD_LIBRARY[game.selectedCard].description;
  } else {
    $("#turnPill").textContent = "你的回合";
    $("#actionTitle").textContent = game.hand.length ? "轮到你了，谨慎出牌" : "先领取你的手牌";
    $("#actionHint").textContent = game.drewThisTurn || !game.canDraw ? "本回合已抽牌，可选牌或结束回合。" : `可抽取 ${game.drawCountNext} 张牌${game.hand.length ? "，或选择一张手牌使用。" : "，开始对局。"}`;
  }
}

function setBusy(busy) { game.busy = busy; syncControls(); }
function showError(error) { setStatus(error.name === "TimeoutError" ? "请求超时，操作结果尚未确认。请稍后刷新并检查服务状态。" : error.message || "操作未完成，请重试。", true); }

async function recoverAction(error) {
  if (error.confirmedRejection) { showError(error); return; }
  // Never replay a mutation after an uncertain response. Reconcile with a read.
  game.recoveryRequired = true;
  try {
    const payload = await api(`/games/${game.gameId}`, { method: "GET" });
    game.drewThisTurn = payload.game.canDraw !== true;
    hydrateGame(payload.game);
    game.recoveryRequired = false;
    setStatus("上次响应中断，已重新读取牌局。请核对当前手牌后再操作；未自动重发出牌或抽牌。", true);
  } catch {
    setStatus("网络中断且无法确认操作结果，已锁定操作以避免重复扣牌。刷新页面将创建新牌局。", true);
  }
}

async function drawCard() {
  if (!canAct() || !game.canDraw || game.drewThisTurn || game.deck <= 0) return;
  try {
    setBusy(true);
    const payload = await api(`/games/${game.gameId}/draw`, { method: "POST" });
    game.drewThisTurn = true;
    hydrateGame(payload.game);
    const drawn = payload.drawnCards || (payload.drawnCard ? [payload.drawnCard] : []);
    const names = drawn.map((id) => `「${CARD_LIBRARY[id]?.name || "未知卡牌"}」`).join("、");
    setStatus(payload.exploded ? "你抽到了炸弹猫且没有拆弹，本局已结束。" : payload.defused ? "你抽到了炸弹猫，已使用拆弹成功化解。" : `你抽取了 ${drawn.length} 张牌${names ? `：${names}` : ""}。可以选牌或结束回合。`);
  } catch (error) { await recoverAction(error); } finally { setBusy(false); }
}

async function endTurn() {
  if (!canAct() || !game.hand.length) return;
  try {
    setBusy(true);
    if (game.selectedCard) {
      const played = await api(`/games/${game.gameId}/play`, { method: "POST", body: JSON.stringify({ cardId: game.selectedCard }) });
      hydrateGame(played.game);
    }
    if (game.gameOver) return;
    setStatus("其他 AI 对手正在依次行动，手牌数量将在服务端返回后更新…");
    const payload = await api(`/games/${game.gameId}/end-turn`, { method: "POST" });
    game.drewThisTurn = false;
    hydrateGame(payload.game);
    setStatus(game.gameOver ? "本局已结束，请查看对局动态。" : "其他对手已完成行动，轮到你了！");
  } catch (error) { await recoverAction(error); } finally { setBusy(false); }
}

async function startGame() {
  render();
  setBusy(true);
  try {
    const payload = await api("/games", { method: "POST" });
    hydrateGame(payload.game);
    setStatus(`五人牌局已就绪。点击抽牌堆，一次领取 ${game.drawCountNext} 张手牌。`);
  } catch (error) {
    $("#connectionBadge").className = "connection-badge is-error";
    $("#connectionBadge span").textContent = "未连接服务";
    setStatus("无法连接游戏后端。四个座位已保留，请启动游戏服务后刷新；直接打开 HTML 只能查看界面。", true);
  } finally {
    setBusy(false);
    if (!game.connected) {
      $("#actionTitle").textContent = "等待游戏服务";
      $("#actionHint").textContent = "请启动现有后端并刷新页面。";
    }
  }
}

function openSettings() {
  previouslyFocused = document.activeElement;
  $("#settingsModal").hidden = false;
  $(".modal-close").focus();
}
function closeSettings() {
  $("#settingsModal").hidden = true;
  previouslyFocused?.focus();
}
$("#deckButton").addEventListener("click", drawCard);
$("#drawButton").addEventListener("click", drawCard);
$("#endButton").addEventListener("click", endTurn);
$("#settingsButton").addEventListener("click", openSettings);
document.querySelectorAll("[data-close-settings]").forEach((button) => button.addEventListener("click", closeSettings));
$("#saveApiButton").addEventListener("click", closeSettings);
$("#clearLog").addEventListener("click", () => { hiddenLogPrefix = [...game.logs]; renderLog(); });
document.addEventListener("keydown", (event) => {
  if (!$("#settingsModal").hidden) {
    if (event.key === "Escape") closeSettings();
    if (event.key === "Tab") {
      const focusable = [...$(".modal-card").querySelectorAll("button, input")];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    return;
  }
  // Let focused buttons handle Enter themselves, avoiding a second draw on keydown.
  if (event.key === "Enter" && !event.repeat && [document.body, document.documentElement].includes(document.activeElement)) {
    event.preventDefault();
    drawCard();
  }
});
startGame();
