const $ = (id) => document.getElementById(id);
let csrf = "";
let currentState = null;
let channelRows = [];
let lastStateAt = 0;
let statusUnavailable = true;
let stateRequest = null;
let currentView = "overview";
const STALE_AFTER = 35000;
function statusNotice(message) {
  $("connection-notice").textContent = message;
  $("connection-notice").classList.toggle("hidden", !message);
}
function staleStatus(message) {
  statusUnavailable = true;
  statusNotice(message);
  if (currentState) renderState(currentState);
}
function renderChatLists() {
  const chosen = channelRows.filter(row => row.source === "manual");
  const ids = currentState?.health?.twitch?.active_logins || [];
  const active = statusUnavailable || !currentState?.health?.fresh || !currentState?.health?.twitch_online ? [] : ids.map(login => ({login, source: "catalog"}));
  renderChannelTable(chosen, active);
}
async function refreshVisible() {
  await loadState();
  if (currentView === "overview") await loadMatches();
  if (currentView === "activity" || currentView === "channels") await loadChannels();
}
let pendingServiceAction = "restart";
let toastTimer;
let refreshTimer;
let catalogPage = 0;
let catalogCsv = "";
let previewMode = "";
let previewRevision = 0;
let catalogRequest = 0;

const labels = {
  overview: "Обзор",
  activity: "Активность",
  channels: "Каналы",
  catalog: "База поиска",
  words: "Фразы",
  logs: "Журнал",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  let response;
  let payload = {};
  try {
    response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers, signal: controller.signal });
    try { payload = await response.json(); } catch { /* invalid response is handled below */ }
  } catch (error) {
    throw new Error(error.name === "AbortError" ? "Сервер не ответил за 12 секунд. Повтори попытку." : "Нет связи с панелью. Проверь подключение к интернету.");
  } finally { clearTimeout(timeout); }

  if (response.status === 401 && path !== "/api/login" && path !== "/api/setup") {
    showAuth(false);
    throw new Error("Сеанс завершён. Войди в панель снова.");
  }
  if (!response.ok) throw new Error(payload.error || "Не удалось выполнить запрос");
  return payload;
}

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.classList.remove("show"); node.textContent = ""; }, 3200);
}

function showAuth(setupRequired) {
  clearInterval(refreshTimer);
  if ($("confirm-modal").open) $("confirm-modal").close();
  $("app").classList.add("hidden");
  $("auth").classList.remove("hidden");
  $("auth-error").textContent = "";
  $("setup-form").classList.toggle("hidden", !setupRequired);
  $("login-form").classList.toggle("hidden", setupRequired);
  $("auth-title").textContent = setupRequired ? "Первичная настройка" : "Панель управления";
  $("auth-copy").textContent = setupRequired
    ? "Введи одноразовый код из Telegram и задай пароль для сайта."
    : "Войди, чтобы управлять ботом и смотреть состояние сервера.";
}

async function initialize() {
  try {
    const boot = await api("/api/bootstrap");
    const session = await fetch("/api/session", { credentials: "same-origin", cache: "no-store" });
    if (session.ok) {
      const data = await session.json();
      csrf = data.csrf;
      showApp();
      return;
    }
    showAuth(boot.setup_required || session.status === 409);
  } catch (error) {
    showAuth(false);
    $("auth-error").textContent = error.message || "Не удалось подключиться к панели";
  }
}

async function submitLogin(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const result = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("login-password").value }),
    });
    csrf = result.csrf;
    $("login-form").reset();
    showApp();
  } catch (error) {
    $("auth-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function submitSetup(event) {
  event.preventDefault();
  const bootstrapToken = $("bootstrap-token").value;
  const password = $("new-password").value;
  if (password.length < 16) {
    $("auth-error").textContent = "Пароль должен содержать не менее 16 символов.";
    return;
  }
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("/api/setup", { method: "POST", body: JSON.stringify({ bootstrap_token: bootstrapToken, password }) });
    $("setup-form").reset();
    showAuth(false);
    $("auth-error").textContent = "Пароль создан. Теперь войди в панель.";
  } catch (error) {
    $("auth-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function showApp() {
  $("restart-button").disabled = true;
  $("service-toggle").disabled = true;
  statusNotice("Подключаюсь к панели и проверяю состояние бота…");
  $("auth").classList.add("hidden");
  $("app").classList.remove("hidden");
  loadAll().catch((error) => toast(error.message, true));
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refreshVisible().catch(error => toast(error.message, true)), 15000);
}

function navigate(view) {
  if (!labels[view]) return;
  currentView = view;
  document.title = `${labels[view]} — Twitch Watcher`;
  document.querySelectorAll(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  document.querySelectorAll(".nav-item[data-view]").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  document.querySelectorAll(".nav-item[data-view]").forEach(node => {
    if (node.dataset.view === view) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  const heading = $(`view-${view}`).querySelector("h1");
  $("view-heading")?.removeAttribute("id");
  heading.id = "view-heading";
  heading.focus({preventScroll:true});
  $("page-crumb").textContent = labels[view];
  if (view === "overview") loadMatches().catch(error => toast(error.message,true));
  window.scrollTo(0, 0);
  if (view === "activity") loadChannels().catch((error) => toast(error.message, true));
  if (view === "channels") loadChannels().catch((error) => toast(error.message, true));
  if (view === "words") loadWords().catch((error) => toast(error.message, true));
  if (view === "words") loadFilters().catch((error) => toast(error.message, true));
  if (view === "catalog") loadCatalog().catch((error) => toast(error.message, true));
  if (view === "logs") loadLogs().catch((error) => toast(error.message, true));
}

async function loadAll() {
  await Promise.all([loadState(), loadChannels(), loadWords(), loadMatches(), loadCatalog(), loadFilters()]);
}

async function loadState() {
  if (stateRequest) return stateRequest;
  stateRequest = (async () => {
    try {
      const result = await api("/api/state");
      currentState = result.data;
      lastStateAt = Date.now();
      statusUnavailable = false;
      statusNotice("");
      renderState(currentState);
    } catch (error) {
      staleStatus("Нет свежих данных от панели. Последнее состояние не подтверждено. " + error.message);
      throw error;
    }
  })();
  try { return await stateRequest; } finally { stateRequest = null; }
}
setInterval(() => {
  if (lastStateAt && !statusUnavailable && Date.now() - lastStateAt > STALE_AFTER)
    staleStatus("Данные устарели. Связь с панелью проверяется; текущее состояние бота неизвестно.");
}, 1000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || $("app").classList.contains("hidden")) return;
  if (lastStateAt && Date.now() - lastStateAt > STALE_AFTER) staleStatus("Обновляю устаревшие данные…");
  refreshVisible().catch(error => toast(error.message,true));
});

function formatBytes(value) {
  if (!value) return "0 ГБ";
  return `${(value / 1024 ** 3).toFixed(1)} ГБ`;
}

function renderState(data) {
  const active = data.service === "active";
  const health = statusUnavailable ? {...data.health, state:"stale", fresh:false, twitch_online:false, telegram_online:false, active_chats:0} : (data.health || {});
  $("restart-button").disabled = statusUnavailable;
  $("service-toggle").disabled = statusUnavailable;
  document.querySelector(".hero-card").dataset.state = health.state || "stale";
  const states = { healthy: "работает", waiting: "ожидает активных каналов", degraded: "требует внимания", stale: "не передаёт свежий статус", stopped: "не запущен" };
  $("service-label").textContent = statusUnavailable ? "НЕТ СВЕЖИХ ДАННЫХ" : active ? "СЛУЖБА РАБОТАЕТ" : "СЛУЖБА ОСТАНОВЛЕНА";
  $("hero-state").textContent = states[health.state] || "проверяется";
  $("hero-note").textContent = health.fresh
    ? "Статус основан на свежем сигнале бота, состоянии API и подтверждённых подписках Twitch."
    : "Наличие работающей службы ещё не подтверждает чтение чатов. Свежей диагностики нет.";
  $("bot-version").textContent = `v${health.version || data.version || "—"}`;
  $("twitch-health").textContent = health.twitch_online ? "На связи" : (health.state === "waiting" ? "Нет активных выбранных чатов" : "Нет свежего соединения");
  $("telegram-health").textContent = health.telegram_online ? "API доступен" : "Нет свежего подтверждения";
  $("chat-health").textContent = `${health.active_chats ?? 0} / ${health.desired_chats ?? 0} · лимит 100`;
  $("queue-health").textContent = String(data.queued_notifications ?? 0);
  $("live-health").textContent = String(health.live_channels ?? 0);
  $("monitoring-label").textContent = health.twitch_online ? "Чтение чатов подтверждено" : (health.state === "waiting" ? "Ожидание активных чатов" : "Нет свежего подтверждения чтения");
  $("heartbeat-health").textContent = health.heartbeat_at ? new Date(health.heartbeat_at * 1000).toLocaleTimeString("ru-RU") : "Нет";
  const errors = [health.twitch, health.scanner, health.telegram, health.delivery, health.activity, health.processing].filter((part) => part?.error).map((part) => part.error);
  $("health-error").textContent = errors.length ? `Ошибка: ${[...new Set(errors)].join(" · ")}` : "";
  $("service-label").style.color = statusUnavailable ? "#ffd183" : active ? "#a2f0cf" : "#ff9eae";
  $("restart-button").textContent = active ? "↻ Перезапустить" : "▶ Запустить";
  $("service-toggle").textContent = active ? "Остановить" : "Перезапустить";
  $("service-toggle").dataset.action = active ? "stop" : "restart";

  const ramUsed = data.ram_used || 0;
  const ramTotal = data.ram_total || 0;
  const diskUsed = data.disk_used || 0;
  const diskTotal = data.disk_total || 0;
  const ramPct = ramTotal ? Math.round((ramUsed / ramTotal) * 100) : 0;
  const diskPct = diskTotal ? Math.round((diskUsed / diskTotal) * 100) : 0;
  $("ram-value").textContent = `${ramPct}%`;
  $("ram-sub").textContent = `${formatBytes(ramUsed)} из ${formatBytes(ramTotal)} использовано`;
  $("ram-meter").style.width = `${ramPct}%`;
  $("disk-value").textContent = `${diskPct}%`;
  $("disk-sub").textContent = `${formatBytes(diskUsed)} из ${formatBytes(diskTotal)} занято`;
  $("disk-meter").style.width = `${diskPct}%`;
  $("channels-value").textContent = String(health.active_chats ?? 0);
  $("channels-sub").textContent = `${data.enabled_catalog_channels ?? 0} включено из ${data.catalog_channels ?? 0} в базе`;
  $("words-value").textContent = String(data.phrases ?? 0);
  $("matches-value").textContent = String(data.matches ?? 0);
  $("active-since").textContent = data.active_since || "—";
  $("restart-count").textContent = String(data.restarts ?? "—");
  $("load-value").textContent = (data.load || []).join(" · ") || "—";
  $("manual-count").textContent = String(data.chosen_channels ?? 0);
  $("auto-count").textContent = String(health.active_chats ?? 0);
  renderChatLists();
  const date = new Date(data.checked_at);
  $("updated-at").textContent = Number.isNaN(date.valueOf()) ? "сейчас" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

async function loadChannels() {
  const result = await api("/api/channels");
  const rows = result.data || [];
  const chosen = rows.filter((row) => row.source === "manual");
  channelRows = rows;
  $("channel-count").textContent = `${chosen.length} выбрано`;
  $("activity-count").textContent = `${chosen.length} каналов`;
  renderChatLists();
  renderActivity(chosen);
}

function renderChannelTable(chosen, automatic) {
  const list = $("channel-table");
  if (!chosen.length && !automatic.length) {
    list.innerHTML = '<div class="empty-state">Пока нет каналов. Добавь логин выше или открой Telegram-бота.</div>';
    return;
  }
  const makeRow = (channel) => {
    const manual = channel.source === "manual";
    const live = Boolean(channel.is_live);
    const detail = [channel.category, channel.title].filter(Boolean).join(" · ");
    return `<div class="data-row"><div class="channel-avatar">${escapeHtml(channel.login.slice(0,1).toUpperCase())}</div><div class="data-primary"><b>@${escapeHtml(channel.login)}</b><small>${escapeHtml(detail || (manual ? "Уведомления об активности включены" : "Активный чат из базы поиска"))}</small></div><span class="status-chip ${manual ? (live ? "live" : "offline") : "auto"}">${manual ? (live ? "В эфире" : "Выбран") : "Из базы"}</span>${manual ? `<button class="icon-action" data-delete-channel="${escapeHtml(channel.login)}" aria-label="Удалить канал ${escapeHtml(channel.login)}" title="Удалить канал">Удалить</button>` : ""}</div>`;
  };
  list.innerHTML = `<div class="group-label">ВЫБРАНЫ ТОБОЙ · УВЕДОМЛЕНИЯ ОБ АКТИВНОСТИ</div>${chosen.map(makeRow).join("") || '<div class="empty-state compact">Список выбранных каналов пуст.</div>'}<div class="group-label">ПОДТВЕРЖДЁННЫЕ ЧАТЫ · ${automatic.length} ИЗ 100</div>${automatic.map(makeRow).join("") || '<div class="empty-state compact">Нет подтверждённых подписок. Проверь живую диагностику на странице обзора.</div>'}`;
  list.querySelectorAll("[data-delete-channel]").forEach((button) => button.addEventListener("click", () => mutateChannel("delete", button.dataset.deleteChannel)));
}

function renderActivity(channels) {
  const list = $("activity-list");
  if (!channels.length) {
    list.innerHTML = '<div class="empty-state">Добавь каналы, за которыми хочешь следить. Уведомления будут только по выбранным каналам.</div>';
    return;
  }
  list.innerHTML = channels.map((channel) => {
    const live = Boolean(channel.is_live);
    const details = [channel.category, channel.title].filter(Boolean).join(" · ");
    return `<article class="activity-card"><span class="activity-status">${live ? "🔴" : "⚫"}</span><div><b>@${escapeHtml(channel.login)} · ${live ? "В эфире" : "Не в эфире"}</b><small>${escapeHtml(details || "Статус сохранится после первой проверки")}</small></div></article>`;
  }).join("");
}

async function loadWords() {
  const result = await api("/api/words");
  const words = result.data || [];
  $("word-count").textContent = `${words.length} фраз`;
  const list = $("word-list");
  if (!words.length) {
    list.innerHTML = '<div class="empty-state">Список пуст. Добавь первое слово или фразу.</div>';
    return;
  }
  list.innerHTML = words.map((word) => `<div class="data-row"><div class="quick-icon amber">⌕</div><div class="data-primary"><b>${escapeHtml(word)}</b><small>Ищется с учётом границ слов</small></div><button class="icon-action" data-delete-word="${escapeHtml(word)}" aria-label="Удалить фразу ${escapeHtml(word)}" title="Удалить фразу">Удалить</button></div>`).join("");
  list.querySelectorAll("[data-delete-word]").forEach((button) => button.addEventListener("click", () => mutateWord("delete", button.dataset.deleteWord)));
}

async function loadMatches() {
  const result = await api("/api/matches");
  const items = result.data || [];
  const list = $("matches-list");
  if (!items.length) {
    list.innerHTML = '<div class="empty-state">Совпадений пока нет. Когда фраза найдётся в чате, событие появится здесь.</div>';
    return;
  }
  list.innerHTML = items.slice(0,8).map((item) => {
    const date = new Date(item.created_at);
    const stamp = Number.isNaN(date.valueOf()) ? "" : date.toLocaleString("ru-RU", { day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit" });
    return `<article class="match-row"><div class="match-row-top"><b>${escapeHtml(item.matched_rule)} · @${escapeHtml(item.channel_login)}</b><time>${escapeHtml(stamp)}</time></div><p>@${escapeHtml(item.author_login)}: ${escapeHtml(item.message)}</p></article>`;
  }).join("");
}

async function loadLogs() {
  const result = await api("/api/logs");
  $("log-output").textContent = (result.data || []).join("\n") || "Записей за последние сутки нет.";
}

async function mutateWord(action, phrase) {
  try {
    await api("/api/words", { method: "POST", body: JSON.stringify({ action, phrase }) });
    await Promise.all([loadWords(), loadState()]);
    toast(action === "add" ? "Фраза добавлена" : "Фраза удалена");
  } catch (error) { toast(error.message, true); }
}

async function mutateChannel(action, login) {
  try {
    await api("/api/channels", { method: "POST", body: JSON.stringify({ action, login }) });
    $("channel-input").value = "";
    await Promise.all([loadChannels(), loadState()]);
    toast(action === "add" ? `Канал @${login} добавлен` : `Канал @${login} удалён`);
  } catch (error) { toast(error.message, true); }
}

function openConfirm(action) {
  pendingServiceAction = action;
  const title = action === "stop" ? "Остановить бота?" : action === "start" ? "Запустить бота?" : "Перезапустить бота?";
  const copy = action === "stop"
    ? "Поиск и Telegram-уведомления приостановятся. Панель сайта останется доступна."
    : "Служба будет запущена через несколько секунд. Панель сайта останется доступна.";
  $("confirm-title").textContent = title;
  $("confirm-copy").textContent = copy;
  $("confirm-yes").textContent = action === "stop" ? "Остановить" : action === "start" ? "Запустить" : "Перезапустить";
  $("confirm-modal").showModal();
}

async function runServiceAction() {
  $("confirm-yes").disabled = true;
  try {
    await api("/api/service", { method: "POST", body: JSON.stringify({ action: pendingServiceAction }) });
    $("confirm-modal").close();
    toast(pendingServiceAction === "stop" ? "Остановка запланирована" : "Команда принята · служба запустится через пару секунд");
    setTimeout(() => loadState().catch(() => {}), 5000);
  } catch (error) { toast(error.message, true); }
  finally { $("confirm-yes").disabled = false; }
}

async function logout() {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch { /* cookie may already be expired */ }
  csrf = "";
  catalogCsv = "";
  $("catalog-file").value = "";
  $("catalog-apply").disabled = true;
  showAuth(false);
}

async function loadCatalog() {
  const request = ++catalogRequest;
  const params = new URLSearchParams({ search: $("catalog-search").value.trim(), enabled: $("catalog-enabled").value, page: String(catalogPage) });
  const result = await api(`/api/catalog?${params}`);
  if (request !== catalogRequest) return;
  const data = result.data;
  catalogPage = data.page;
  $("catalog-count").textContent = `${data.total} найдено`;
  $("catalog-page").textContent = `${data.page + 1} / ${data.pages}`;
  $("catalog-prev").disabled = data.page <= 0;
  $("catalog-next").disabled = data.page + 1 >= data.pages;
  const list = $("catalog-list");
  list.innerHTML = data.items.map((row) => `<div class="catalog-row ${row.enabled ? "" : "is-disabled"}" data-catalog-login="${escapeHtml(row.login)}"><b class="catalog-login">@${escapeHtml(row.login)}</b><label>Поиск <input class="catalog-enabled" type="checkbox" ${row.enabled ? "checked" : ""} aria-label="Искать в ${escapeHtml(row.login)}"></label><label>Приоритет <input class="catalog-priority" type="number" min="1" max="2147483647" step="1" value="${row.priority}" aria-label="Приоритет ${escapeHtml(row.login)}"></label><button class="button primary catalog-save" aria-label="Сохранить ${escapeHtml(row.login)}" type="button">Сохранить</button></div>`).join("") || '<div class="empty-state">По этому фильтру каналов нет.</div>';
  list.querySelectorAll(".catalog-save").forEach((button) => button.addEventListener("click", async () => {
    const row = button.closest("[data-catalog-login]");
    button.disabled = true;
    try {
      await api("/api/catalog", { method: "POST", body: JSON.stringify({ action: "update", login: row.dataset.catalogLogin, priority: Number(row.querySelector(".catalog-priority").value), enabled: row.querySelector(".catalog-enabled").checked }) });
      await Promise.all([loadCatalog(), loadState()]);
      toast("Сохранено. Применится при следующей проверке каналов.");
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }));
}

async function loadFilters() {
  const result = await api("/api/filters");
  const data = result.data;
  $("cooldown-seconds").value = data.repeat_cooldown_seconds;
  $("exclude-list").innerHTML = data.excluded_words.map((phrase) => `<div class="data-row"><div class="data-primary"><b>${escapeHtml(phrase)}</b></div><button class="icon-action" type="button" data-delete-exclude="${escapeHtml(phrase)}">Удалить</button></div>`).join("") || '<p class="muted">Исключений нет.</p>';
  $("exclude-list").querySelectorAll("[data-delete-exclude]").forEach((button) => button.addEventListener("click", () => mutateFilter({ action: "delete", phrase: button.dataset.deleteExclude }).catch((error) => toast(error.message, true))));
}

async function mutateFilter(payload) {
  await api("/api/filters", { method: "POST", body: JSON.stringify(payload) });
  await loadFilters();
  toast("Фильтры обновлены. Поиск учтёт их в течение нескольких секунд.");
}

document.querySelectorAll("[data-view]").forEach((node) => node.addEventListener("click", () => navigate(node.dataset.view)));
$("login-form").addEventListener("submit", submitLogin);
$("setup-form").addEventListener("submit", submitSetup);
$("refresh").addEventListener("click", () => loadAll().then(() => toast("Данные обновлены")).catch((error) => toast(error.message, true)));
$("activity-refresh").addEventListener("click", () => loadChannels().then(() => toast("Список обновлён")).catch((error) => toast(error.message, true)));
$("logs-refresh").addEventListener("click", () => loadLogs().then(() => toast("Журнал обновлён")).catch((error) => toast(error.message, true)));
$("restart-button").addEventListener("click", () => openConfirm(currentState?.service === "active" ? "restart" : "start"));
$("service-toggle").addEventListener("click", (event) => openConfirm(event.currentTarget.dataset.action || "stop"));
$("confirm-cancel").addEventListener("click", () => $("confirm-modal").close());
$("confirm-yes").addEventListener("click", runServiceAction);

$("logout").addEventListener("click", logout);
$("mobile-logout").addEventListener("click", logout);
$("catalog-search-form").addEventListener("submit", (event) => { event.preventDefault(); catalogPage = 0; loadCatalog().catch((error) => toast(error.message, true)); });
$("catalog-prev").addEventListener("click", () => { catalogPage = Math.max(0, catalogPage - 1); loadCatalog().catch((error) => toast(error.message, true)); });
$("catalog-next").addEventListener("click", () => { catalogPage += 1; loadCatalog().catch((error) => toast(error.message, true)); });
$("catalog-add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    await api("/api/catalog", { method: "POST", body: JSON.stringify({ action: "add", login: $("catalog-add-login").value.trim(), priority: Number($("catalog-add-priority").value) }) });
    $("catalog-add-login").value = "";
    await Promise.all([loadCatalog(), loadState()]);
    toast("Канал добавлен в базу поиска.");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
function invalidatePreview() { previewRevision += 1; catalogCsv = ""; previewMode = ""; $("catalog-apply").disabled = true; $("catalog-preview-info").textContent = "Проверь CSV перед импортом."; }
$("catalog-file").addEventListener("change", invalidatePreview);
$("catalog-import-mode").addEventListener("change", invalidatePreview);
$("catalog-import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  invalidatePreview();
  const revision = previewRevision;
  const file = $("catalog-file").files[0];
  if (!file || file.size > 2000000) { toast("Выбери CSV размером до 2 МБ.", true); return; }
  $("catalog-preview").disabled = true;
  try {
    const mode = $("catalog-import-mode").value;
    const text = await file.text();
    if (revision !== previewRevision) return;
    const result = await api("/api/catalog/preview", { method: "POST", body: JSON.stringify({ csv: text, mode }) });
    if (revision !== previewRevision) return;
    catalogCsv = text; previewMode = mode;
    $("catalog-preview-info").textContent = `Каналов в файле: ${result.data.incoming}. Новых: ${result.data.added}. Будет удалено из каталога: ${result.data.removed}. История и ручные уведомления сохранятся.`;
    $("catalog-apply").disabled = false;
  } catch (error) { invalidatePreview(); toast(error.message, true); }
  finally { $("catalog-preview").disabled = false; }
});
$("catalog-apply").addEventListener("click", async () => {
  if (!catalogCsv || previewMode !== $("catalog-import-mode").value) return;
  if (previewMode === "replace" && !window.confirm("Заменить весь каталог поиска каналами из CSV? Каналы вне файла исчезнут из базы поиска. История, правила и ручные уведомления сохранятся.")) return;
  $("catalog-apply").disabled = true;
  try {
    await api("/api/catalog/import", { method: "POST", body: JSON.stringify({ csv: catalogCsv, mode: previewMode, confirm: previewMode === "replace" }) });
    invalidatePreview(); catalogPage = 0;
    await Promise.all([loadCatalog(), loadState()]);
    toast("Каталог импортирован.");
  } catch (error) { $("catalog-apply").disabled = false; toast(error.message, true); }
});
$("exclude-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await mutateFilter({ action: "add", phrase: $("exclude-input").value.trim() }); $("exclude-input").value = ""; }
  catch (error) { toast(error.message, true); }
});
$("cooldown-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await mutateFilter({ action: "cooldown", seconds: Number($("cooldown-seconds").value) }); }
  catch (error) { toast(error.message, true); }
});

$("word-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const phrase = $("word-input").value.trim();
  if (!phrase) return;
  await mutateWord("add", phrase);
  $("word-input").value = "";
});
$("channel-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const login = $("channel-input").value.trim();
  if (login) await mutateChannel("add", login);
});

initialize();
