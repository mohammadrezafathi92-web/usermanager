import axios from "axios";

const client = axios.create({ baseURL: "/api" });

client.interceptors.request.use((config) => {
  const token = localStorage.getItem("um_token");
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

client.interceptors.response.use(
  (res) => res,
  (err) => {
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
export const updateConnection = (userId, connectionId, data) =>
  client.put(`/users/${userId}/connections/${connectionId}`, data);
export const unbanConnection = (userId, connectionId) =>
  client.post(`/users/${userId}/connections/${connectionId}/unban`);
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

export const fetchTelegramBotSettings = () => client.get("/telegram-bot");
export const updateTelegramBotSettings = (data) => client.put("/telegram-bot", data);
export const restartTelegramBot = () => client.post("/telegram-bot/restart");

export const fetchBackups = () => client.get("/backup/list");
export const runBackup = () => client.post("/backup/run", null, { responseType: "blob" });
export const restoreBackup = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return client.post("/backup/restore", fd, { headers: { "Content-Type": "multipart/form-data" }, timeout: 60000 });
};
export const downloadBackup = (filename) =>
  client.get(`/backup/download/${encodeURIComponent(filename)}`, { responseType: "blob" });

export const fetchRemoteBotStatus = () => client.get("/remote-bot/status");
export const deployRemoteBot = (data) => client.post("/remote-bot/deploy", data, { timeout: 320000 });
export const stopRemoteBot = (sshPassword) =>
  client.post("/remote-bot/stop", { ssh_password: sshPassword }, { timeout: 60000 });

export const fetchAdmins = () => client.get("/admins");
export const fetchPermissionChoices = () => client.get("/admins/permission-choices");
export const createAdmin = (data) => client.post("/admins", data);
export const updateAdmin = (id, data) => client.put(`/admins/${id}`, data);
export const deleteAdmin = (id) => client.delete(`/admins/${id}`);

export const fetchAdminGroups = () => client.get("/admins/groups");
export const createAdminGroup = (data) => client.post("/admins/groups", data);
export const updateAdminGroup = (id, data) => client.put(`/admins/groups/${id}`, data);
export const deleteAdminGroup = (id) => client.delete(`/admins/groups/${id}`);

export const topupAdminBalance = (id, data) => client.post(`/admins/${id}/topup`, data);
export const fetchAdminBalanceLogs = (id) => client.get(`/admins/${id}/balance-logs`);

export const topupAdminVolume = (id, data) => client.post(`/admins/${id}/volume-topup`, data);
export const fetchAdminVolumeLogs = (id) => client.get(`/admins/${id}/volume-logs`);

export const fetchAdminLoginLogs = (params) => client.get("/admins/login-logs", { params });

// RADIUS concurrent-session-limit reject/ban history - either the whole
// panel-wide page (no user_id) or scoped to one user (UserDetail.jsx).
export const fetchRadiusLimitLogs = (params) => client.get("/radius-logs", { params });

// Discount/promo codes (کد تخفیف) - panel-wide, see routers/discount_codes.py.
export const fetchDiscountCodes = () => client.get("/discount-codes");
export const createDiscountCode = (data) => client.post("/discount-codes", data);
export const updateDiscountCode = (id, data) => client.put(`/discount-codes/${id}`, data);
export const deleteDiscountCode = (id) => client.delete(`/discount-codes/${id}`);
export const fetchDiscountCodeRedemptions = (id) => client.get(`/discount-codes/${id}/redemptions`);
