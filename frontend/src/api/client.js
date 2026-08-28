import axios from "axios";

const client = axios.create({ baseURL: "/api" });

// --- password-confirmed deletes -------------------------------------
// The backend re-checks the admin's own password on every destructive
// endpoint (see deps.require_confirm_password). Rather than wiring a
// dialog into each of the ~17 delete buttons, this interceptor asks once
// per delete via the prompt that components/ConfirmPasswordGate.jsx
// registers here, so any delete added later is covered automatically.
let _passwordPrompt = null;
export const setPasswordPrompt = (fn) => {
  _passwordPrompt = fn;
};

// Remembered briefly so deleting several things in a row doesn't re-ask
// on every single click. Kept in memory only - never persisted anywhere.
let _cachedPassword = null;
let _cachedAt = 0;
const PASSWORD_TTL_MS = 2 * 60 * 1000;

export const forgetConfirmPassword = () => {
  _cachedPassword = null;
  _cachedAt = 0;
};

const needsConfirmPassword = (config) => {
  const method = (config.method || "").toLowerCase();
  if (method === "delete") return true;
  // Bulk user deletion is a POST by necessity (it carries a body), but
  // it's the single most destructive action in the panel.
  return method === "post" && (config.url || "").includes("/bulk-delete");
};

client.interceptors.request.use(async (config) => {
  const token = localStorage.getItem("um_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;

  if (needsConfirmPassword(config) && !config.headers["X-Confirm-Password"]) {
    const fresh = _cachedPassword && Date.now() - _cachedAt < PASSWORD_TTL_MS;
    if (fresh) {
      config.headers["X-Confirm-Password"] = _cachedPassword;
    } else if (_passwordPrompt) {
      // Throwing here aborts the request cleanly when the admin cancels.
      const pw = await _passwordPrompt({ retry: config._pwRetry === true });
      _cachedPassword = pw;
      _cachedAt = Date.now();
      config.headers["X-Confirm-Password"] = pw;
    }
  }
  return config;
});

client.interceptors.response.use(
  (res) => res,
  async (err) => {
    // A rejected confirm-password means the admin typed it wrong - drop the
    // cached value and ask again once, instead of surfacing a bare 403.
    if (err.response && err.response.status === 403 && err.config && !err.config._pwRetry
        && needsConfirmPassword(err.config)) {
      forgetConfirmPassword();
      err.config._pwRetry = true;
      delete err.config.headers["X-Confirm-Password"];
      try {
        return await client.request(err.config);
      } catch (retryErr) {
        return Promise.reject(retryErr);
      }
    }
    if (err.response && err.response.status === 401) {
      localStorage.removeItem("um_token");
      if (!location.pathname.includes("/login")) {
        location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);

export default client;

// ---------- helpers ----------
export const login = (username, password) => {
  const form = new URLSearchParams();
  form.append("username", username);
  form.append("password", password);
  return axios.post("/api/auth/login", form, {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
};

export const fetchMe = () => client.get("/auth/me");
export const changePassword = (old_password, new_password) =>
  client.post("/auth/change-password", { old_password, new_password });

export const fetchDashboard = () => client.get("/dashboard/stats");

export const fetchUsers = (page = 1, pageSize = 50, search = "", extra = {}) =>
  client.get("/users", {
    params: {
      page,
      page_size: pageSize,
      search: search || undefined,
      status: extra.status || undefined,
      online_only: extra.onlineOnly || undefined,
      sort_by: extra.sortBy || undefined,
      sort_dir: extra.sortDir || undefined,
      owner_admin_id: extra.ownerAdminId || undefined,
      package_id: extra.packageId || undefined,
    },
  });
// Every user id matching the given filters, ignoring pagination - used by
// the "انتخاب همه با این فیلتر" button so a bulk action (e.g. disable/renew
// by package) can target every matching user, not just the current page.
export const fetchUserIds = (search = "", extra = {}) =>
  client.get("/users/ids", {
    params: {
      search: search || undefined,
      status: extra.status || undefined,
      online_only: extra.onlineOnly || undefined,
      owner_admin_id: extra.ownerAdminId || undefined,
      package_id: extra.packageId || undefined,
    },
  });
export const exportUsers = (search = "", extra = {}) =>
  client.get("/users/export", {
    responseType: "blob",
    params: {
      search: search || undefined,
      status: extra.status || undefined,
      online_only: extra.onlineOnly || undefined,
      owner_admin_id: extra.ownerAdminId || undefined,
      package_id: extra.packageId || undefined,
    },
  });
export const fetchUser = (id) => client.get(`/users/${id}`);
export const createUser = (data) => client.post("/users", data);
export const updateUser = (id, data) => client.put(`/users/${id}`, data);
export const deleteUser = (id) => client.delete(`/users/${id}`);
export const resetUsage = (id) => client.post(`/users/${id}/reset-usage`);
export const bulkCreateUsers = (data) => client.post("/users/bulk", data);
export const bulkUpdateUsers = (data) => client.put("/users/bulk", data);
export const bulkDeleteUsers = (userIds) => client.delete("/users/bulk", { data: { user_ids: userIds } });
export const bulkNotifyUsers = (userIds, message) => client.post("/users/bulk-notify", { user_ids: userIds, message });
export const updateConnection = (userId, connectionId, data) =>
  client.put(`/users/${userId}/connections/${connectionId}`, data);
export const unbanConnection = (userId, connectionId) =>
  client.post(`/users/${userId}/connections/${connectionId}/unban`);
export const kickConnection = (userId, connectionId) =>
  client.post(`/users/${userId}/connections/${connectionId}/kick`);
export const applyPackage = (userId, packageId) =>
  client.post(`/users/${userId}/apply-package`, { package_id: packageId });
export const resetPurchaseUsage = (userId, purchaseId) =>
  client.post(`/users/${userId}/purchases/${purchaseId}/reset-usage`);
export const renewPurchase = (userId, purchaseId, data) =>
  client.post(`/users/${userId}/purchases/${purchaseId}/renew`, data);

export const addWireguardConnection = (userId, nodeId, maxConcurrentSessions = 1) =>
  client.post(`/users/${userId}/connections/wireguard`, { node_id: nodeId, max_concurrent_sessions: maxConcurrentSessions });
export const addOpenvpnConnection = (userId, nodeId, maxConcurrentSessions = 1) =>
  client.post(`/users/${userId}/connections/openvpn`, { node_id: nodeId, max_concurrent_sessions: maxConcurrentSessions });
export const addL2tpConnection = (userId, nodeId, maxConcurrentSessions = 1) =>
  client.post(`/users/${userId}/connections/l2tp`, { node_id: nodeId, max_concurrent_sessions: maxConcurrentSessions });
export const addIkev2Connection = (userId, nodeId, maxConcurrentSessions = 1) =>
  client.post(`/users/${userId}/connections/ikev2`, { node_id: nodeId, max_concurrent_sessions: maxConcurrentSessions });
export const addSstpConnection = (userId, nodeId, maxConcurrentSessions = 1) =>
  client.post(`/users/${userId}/connections/sstp`, { node_id: nodeId, max_concurrent_sessions: maxConcurrentSessions });
export const addXrayConnection = (userId, nodeId, flow = "") =>
  client.post(`/users/${userId}/connections/xray`, { node_id: nodeId, flow });
export const deleteConnection = (userId, connectionId) =>
  client.delete(`/users/${userId}/connections/${connectionId}`);
export const getShareLink = (userId, connectionId) =>
  client.get(`/users/${userId}/connections/${connectionId}/share`);

// Customer-facing subscription panel link (public, token-gated - see
// routers/subscription.py + pages/Subscription.jsx). Lazily generated on
// first fetch, so this is safe to call every time UserDetail opens.
export const fetchSubscriptionLink = (userId) => client.get(`/users/${userId}/subscription-link`);
export const regenerateSubscriptionLink = (userId) =>
  client.post(`/users/${userId}/subscription-link/regenerate`);

// The public page itself calls these WITHOUT any auth header - fine, since
// the interceptor above only attaches one if a token happens to be in
// localStorage (e.g. an admin previewing their own panel session) and the
// backend route never checks it either way.
export const fetchPublicSubscriptionInfo = (token) => client.get(`/subscribe/${token}/info`);

export const fetchNodes = () => client.get("/nodes");
export const createNode = (data) => client.post("/nodes", data);
export const updateNode = (id, data) => client.put(`/nodes/${id}`, data);
export const deleteNode = (id) => client.delete(`/nodes/${id}`);
export const testNode = (id) => client.post(`/nodes/${id}/test`);
export const pushRadiusConfig = (id, panelHost, interimUpdate) =>
  client.post(`/nodes/${id}/push-radius-config`, {
    panel_host: panelHost || null,
    interim_update: interimUpdate || "00:05:00",
  });
export const pushSstpConfig = (id, panelHost) =>
  client.post(`/nodes/${id}/push-sstp-config`, { panel_host: panelHost || null });
export const pushL2tpConfig = (id, panelHost) =>
  client.post(`/nodes/${id}/push-l2tp-config`, { panel_host: panelHost || null });
export const pushIkev2Config = (id, panelHost) =>
  client.post(`/nodes/${id}/push-ikev2-config`, { panel_host: panelHost || null });
export const importPppUsers = (id) => client.post(`/nodes/${id}/import-ppp-users`);
export const importUserManagerUsers = (id) => client.post(`/nodes/${id}/import-usermanager-users`);
export const import3xuiClients = (id) => client.post(`/nodes/${id}/import-3xui-clients`);

export const fetchApiKeys = () => client.get("/api-keys");
export const createApiKey = (label) => client.post("/api-keys", { label });
export const toggleApiKey = (id) => client.post(`/api-keys/${id}/toggle`);
export const deleteApiKey = (id) => client.delete(`/api-keys/${id}`);

export const fetchPackages = () => client.get("/packages");
export const createPackage = (data) => client.post("/packages", data);
export const updatePackage = (id, data) => client.put(`/packages/${id}`, data);
export const deletePackage = (id) => client.delete(`/packages/${id}`);
// Seller-only: their own resale price override for a package they can see
// but never edit themselves - price: null clears it (falls back to the
// package's base price again).
export const setMyPackagePrice = (id, price) => client.put(`/packages/${id}/my-price`, { price });

export const uploadPackageFile = (id, file) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post(`/packages/${id}/files`, fd, { headers: { "Content-Type": "multipart/form-data" } });
};
export const deletePackageFile = (id, fileId) => client.delete(`/packages/${id}/files/${fileId}`);

export const fetchTutorials = () => client.get("/tutorials");
export const createTutorial = (data) => client.post("/tutorials", data);
export const updateTutorial = (id, data) => client.put(`/tutorials/${id}`, data);
export const deleteTutorial = (id) => client.delete(`/tutorials/${id}`);

export const uploadTutorialMedia = (id, file) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post(`/tutorials/${id}/media`, fd, { headers: { "Content-Type": "multipart/form-data" } });
};
export const deleteTutorialMedia = (id, mediaId) => client.delete(`/tutorials/${id}/media/${mediaId}`);

export const createTutorialSoftwareLink = (id, data) => client.post(`/tutorials/${id}/software`, data);
export const uploadTutorialSoftwareFile = (id, file, name) => {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("name", name);
  return client.post(`/tutorials/${id}/software/file`, fd, { headers: { "Content-Type": "multipart/form-data" } });
};
export const deleteTutorialSoftware = (id, softwareId) => client.delete(`/tutorials/${id}/software/${softwareId}`);

export const fetchPanelSettings = () => client.get("/settings");
export const updatePanelSettings = (data) => client.put("/settings", data);
export const resolveHaFailover = () => client.post("/ha/resolve");
export const changePanelPort = (newPort) =>
  client.post("/settings/change-port", { new_port: newPort });

// Global card-to-card payment card pool (چند شماره کارت) - manual/rotate/
// threshold mode lives on the settings object above (payment_card_mode/
// active_payment_card_id/payment_card_switch_threshold, saved via
// updatePanelSettings); these just manage the card rows themselves - see
// routers/panel_settings.py's payment-cards routes.
export const fetchPaymentCards = () => client.get("/settings/payment-cards");
export const createPaymentCard = (data) => client.post("/settings/payment-cards", data);
export const updatePaymentCard = (id, data) => client.put(`/settings/payment-cards/${id}`, data);
export const deletePaymentCard = (id) => client.delete(`/settings/payment-cards/${id}`);
export const activatePaymentCard = (id) => client.post(`/settings/payment-cards/${id}/activate`);

// Level-2 Admin's OR level-3 Seller's OWN card-to-card payment info (3-tier
// hierarchy - separate from the global card above, superadmin-excluded) -
// see routers/panel_settings.py's my_payment_router.
export const fetchMyPayment = () => client.get("/settings/my-payment");
export const updateMyPayment = (data) => client.put("/settings/my-payment", data);

// Same per-admin card pool as fetchPaymentCards above, scoped to this
// admin's own bot instead of the global one.
export const fetchMyPaymentCards = () => client.get("/settings/my-payment/cards");
export const createMyPaymentCard = (data) => client.post("/settings/my-payment/cards", data);
export const updateMyPaymentCard = (id, data) => client.put(`/settings/my-payment/cards/${id}`, data);
export const deleteMyPaymentCard = (id) => client.delete(`/settings/my-payment/cards/${id}`);
export const activateMyPaymentCard = (id) => client.post(`/settings/my-payment/cards/${id}/activate`);

export const fetchTelegramBotSettings = () => client.get("/telegram-bot");
export const updateTelegramBotSettings = (data) => client.put("/telegram-bot", data);

// راه‌اندازی خودکار پروکسی تلگرام روی یک نود میکروتیک
export const listTelegramProxyNodes = () => client.get("/telegram-proxy/nodes");
export const checkTelegramProxyNode = (nodeId) => client.post(`/telegram-proxy/check/${nodeId}`);
export const setupTelegramProxy = (data) => client.post("/telegram-proxy/setup", data);
export const disableTelegramProxy = (nodeId) => client.post(`/telegram-proxy/disable/${nodeId}`);

// تونل وایرگارد تلگرام - داخل همین کانتینر بک‌اند بالا می‌آید
export const getTelegramTunnel = () => client.get("/telegram-tunnel");
export const listTunnelNodes = () => client.get("/telegram-tunnel/nodes");
export const setupTelegramTunnel = (data) => client.post("/telegram-tunnel/setup", data);
export const tunnelUp = () => client.post("/telegram-tunnel/up");
export const tunnelDown = () => client.post("/telegram-tunnel/down");
export const tunnelTest = () => client.post("/telegram-tunnel/test");
export const tunnelRefreshCidrs = () => client.post("/telegram-tunnel/refresh-cidrs");
export const restartTelegramBot = () => client.post("/telegram-bot/restart");

// Level-2 Admin's OWN dedicated bot (3-tier hierarchy - separate from the
// shared/global bot above, superadmin-only) - see routers/telegram_bot_settings.py's /my-bot.
export const fetchMyBot = () => client.get("/telegram-bot/my-bot");
export const updateMyBot = (data) => client.put("/telegram-bot/my-bot", data);
export const restartMyBot = () => client.post("/telegram-bot/my-bot/restart");

export const fetchBackups = () => client.get("/backup/list");
export const runBackup = () => client.post("/backup/run", null, { responseType: "blob" });
export const restoreBackup = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post("/backup/restore", fd, { headers: { "Content-Type": "multipart/form-data" }, timeout: 60000 });
};
export const downloadBackup = (filename) =>
  client.get(`/backup/download/${encodeURIComponent(filename)}`, { responseType: "blob" });

// Non-superadmin's OWN scoped backup (their tree only) - see
// routers/backup.py's my_router.
export const fetchMyBackups = () => client.get("/backup/my-backup/list");
export const runMyBackup = () => client.post("/backup/my-backup/run", null, { responseType: "blob" });
export const downloadMyBackup = (filename) =>
  client.get(`/backup/my-backup/download/${encodeURIComponent(filename)}`, { responseType: "blob" });

export const fetchRemoteBotStatus = () => client.get("/remote-bot/status");
// Must stay ABOVE nginx.conf's proxy_read_timeout (660s) for this route -
// otherwise nginx cuts the connection with a bare 504 before this timeout
// ever fires, and axios's OWN timeout error (no err.response at all) hides
// whatever real progress/error the backend had, leaving only the generic
// "خطا در نصب ربات روی سرور دوم" fallback with no detail.
export const deployRemoteBot = (data) => client.post("/remote-bot/deploy", data, { timeout: 720000 });
export const stopRemoteBot = (sshPassword) =>
  client.post("/remote-bot/stop", { ssh_password: sshPassword }, { timeout: 60000 });

export const fetchAdmins = () => client.get("/admins");
export const fetchPermissionChoices = () => client.get("/admins/permission-choices");
export const createAdmin = (data) => client.post("/admins", data);
export const updateAdmin = (id, data) => client.put(`/admins/${id}`, data);
export const deleteAdmin = (id) => client.delete(`/admins/${id}`);

// What deleting this admin would move, and to whom - read-only, shown in
// the confirm dialog. See routers/admins.py's delete_impact.
export const getAdminDeleteImpact = (id) => client.get(`/admins/${id}/delete-impact`);

export const fetchAdminGroups = () => client.get("/admins/groups");
export const createAdminGroup = (data) => client.post("/admins/groups", data);
export const updateAdminGroup = (id, data) => client.put(`/admins/groups/${id}`, data);
export const deleteAdminGroup = (id) => client.delete(`/admins/groups/${id}`);

export const topupAdminBalance = (id, data) => client.post(`/admins/${id}/topup`, data);
export const fetchAdminBalanceLogs = (id) => client.get(`/admins/${id}/balance-logs`);

export const topupAdminVolume = (id, data) => client.post(`/admins/${id}/volume-topup`, data);
export const fetchAdminVolumeLogs = (id) => client.get(`/admins/${id}/volume-logs`);

export const fetchAdminLoginLogs = (params) => client.get("/admins/login-logs", { params });

// 3-tier hierarchy node grants (superadmin only - see routers/admins.py's
// available-nodes/{id}/nodes): which servers a level-2 Admin's own tree
// (themself + their Sellers) is allowed to see/use.
export const fetchAvailableNodesForGrant = () => client.get("/admins/available-nodes");
export const setAdminNodes = (id, nodeIds) => client.put(`/admins/${id}/nodes`, { node_ids: nodeIds });

// Superadmin-only: set an admin's ROLE and its PARENT independently.
// Passing role = undefined leaves the role untouched and moves the account
// only - the backend treats an absent role that way on purpose, so a move
// can never silently change what an account is allowed to do.
export const reparentAdmin = (id, parentAdminId, role) =>
  client.put(`/admins/${id}/reparent`, { parent_admin_id: parentAdminId, role });

// RADIUS concurrent-session-limit reject/ban history - either the whole
// panel-wide page (no user_id) or scoped to one user (UserDetail.jsx).
export const fetchRadiusLimitLogs = (params) => client.get("/radius-logs", { params });

// IP auto-ban list (services/ip_guard.py) - superadmin-only. An IP lands
// here automatically (too many unauthenticated HTTP requests, too many
// RADIUS attempts against an unknown username or a disabled connection -
// exactly the "کاربر ناشناس"/"اتصال غیرفعال" rows on the RADIUS log page)
// or by hand from Settings.
export const fetchIpBans = () => client.get("/ip-bans");
export const addIpBan = (ip, reason) => client.post("/ip-bans", { ip, reason });
export const removeIpBan = (ip) => client.delete(`/ip-bans/${encodeURIComponent(ip)}`);

// Discount/promo codes (کد تخفیف) - panel-wide, see routers/discount_codes.py.
export const fetchDiscountCodes = () => client.get("/discount-codes");
export const createDiscountCode = (data) => client.post("/discount-codes", data);
export const updateDiscountCode = (id, data) => client.put(`/discount-codes/${id}`, data);
export const deleteDiscountCode = (id) => client.delete(`/discount-codes/${id}`);
export const fetchDiscountCodeRedemptions = (id) => client.get(`/discount-codes/${id}/redemptions`);

// ---------- Accounting (حساب‌داری) ----------
export const fetchAccountingSummary = (params = {}) => client.get("/accounting/summary", { params });
export const fetchAccountingSeries = (params = {}) => client.get("/accounting/series", { params });
// One row per direct sub-account: customers, sales, credit, debt. The
// aggregate counterpart to the per-record customer list.
export const fetchAccountingSubtree = (params = {}) => client.get("/accounting/subtree", { params });
export const fetchAccountingTransactions = (params = {}) => client.get("/accounting/transactions", { params });
export const createAccountingExpense = (data) => client.post("/accounting/expenses", data);
export const deleteAccountingExpense = (id) => client.delete(`/accounting/expenses/${id}`);
export const exportAccounting = (params = {}) =>
  client.get("/accounting/export", { params, responseType: "blob" });

// Converts a leftover shared-pool connection group into an independent
// Purchase (see routers/users.py's convert_legacy_group).
export const convertLegacyGroup = (userId, data) => client.post(`/users/${userId}/legacy-groups/convert`, data);

// Live CPU/RAM/disk/uptime for every accessible node (see services/node_monitor.py).
export const fetchNodeResources = () => client.get("/nodes/resources");
// Admin-side edit of a service's free-form label (models.Purchase.comment).
export const updatePurchaseComment = (userId, purchaseId, comment) =>
  client.put(`/users/${userId}/purchases/${purchaseId}/comment`, { comment });
// Re-pushes every stored Xray client back onto a node with its original
// uuid - recovery after the node's panel was wiped/reinstalled.
export const rebuildNodeClients = (id) => client.post(`/nodes/${id}/rebuild-clients`);

// Deletes one of a customer's independent services (its connections are
// removed from the nodes too) - see routers/users.py's delete_purchase.
export const deletePurchase = (userId, purchaseId) =>
  client.delete(`/users/${userId}/purchases/${purchaseId}`);

// ---------- تبلیغات (channel adverts - see backend routers/ads.py) ----------
export const fetchAdChannel = () => client.get("/ads/channel");
export const updateAdChannel = (data) => client.put("/ads/channel", data);
export const fetchAdPlaceholders = () => client.get("/ads/placeholders");
export const fetchAdSchedule = () => client.get("/ads/schedule");
export const fetchAdPosts = () => client.get("/ads/posts");
export const createAdPost = (data) => client.post("/ads/posts", data);
export const updateAdPost = (id, data) => client.put(`/ads/posts/${id}`, data);
export const deleteAdPost = (id) => client.delete(`/ads/posts/${id}`);
export const previewAdPost = (id) => client.get(`/ads/posts/${id}/preview`);
export const sendAdPostNow = (id) => client.post(`/ads/posts/${id}/send`);
export const uploadAdPostImage = (id, file) => {
  const form = new FormData();
  form.append("file", file);
  return client.post(`/ads/posts/${id}/image`, form, { headers: { "Content-Type": "multipart/form-data" } });
};
export const deleteAdPostImage = (id) => client.delete(`/ads/posts/${id}/image`);

// ---------- بروزرسانی پنل (see backend services/self_update.py) ----------
export const checkPanelUpdate = () => client.get("/settings/update/check");
export const applyPanelUpdate = () => client.post("/settings/update/apply", {}, { timeout: 900000 });
