// app.web.js — bản web của ui/app.js
// Thay window.pywebview.api.* bằng REST (fetch) + SSE. Giữ nguyên tên method để index.html không đổi nhiều.

// Đọc JSON an toàn: nếu server trả HTML (413/500/proxy) thì báo lỗi rõ ràng thay vì vỡ JSON.
async function safeJson(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    if (r.status === 413) {
      return { ok: false, msg: 'File tải lên quá lớn — server/proxy từ chối (HTTP 413). Cần tăng giới hạn upload (nginx client_max_body_size).' };
    }
    return { ok: false, msg: `Máy chủ trả về không hợp lệ (HTTP ${r.status || '?'}).` };
  }
}

const api = {
  async get(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    return safeJson(r);
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return safeJson(r);
  },
};

function app() {
  return {
    // ── State ──
    version: '1.4.5',
    service: 'fotello',
    activeTab: 'upload',
    activeLogTab: 'global',

    // Auto-update (không dùng trên web)
    updateAvailable: false,
    latestVersion: '',
    updateNotes: '',
    updateUrl: '',
    isUpdating: false,
    updateProgress: 0,

    // App Login
    isAppLoggedIn: false,
    checkingSession: true,
    appUsername: '',
    loginInputUsername: '',
    loginInputPassword: '',
    appLoginError: '',
    appLoginLoading: false,
    appUserRole: '',
    appUserExpiry: '',
    appUserExpired: false,

    // Connection
    fotelloConnected: false,
    autohdrConnected: false,
    hwid: '',
    loginLoading: false,

    // Connect modal (dán token/cookie)
    showConnectModal: false,
    connectInput: '',
    connectLoading: false,
    connectError: '',

    // AutoEnhance / UpCase (chưa hỗ trợ web — giữ để HTML không lỗi)
    autoenhance: { orderUrl: 'https://app.autoenhance.ai/orders/', saveDir: '', quality: 90, preview: true },
    upcase: {
      checking: false, ready: false, installing: false, device: 'cpu', missing: [],
      modelDir: '', outputDir: '', models: [], preset: 'x4',
      format: 'png', tile: 128, maxSide: 2000, overwrite: false,
      resizeOn: false, width: '', height: '', keepAspect: true,
    },

    // Upload
    selectedFiles: [],        // File[]
    isDragging: false,
    _uploadId: null,          // cache upload_id server-side
    uploadRunning: false,     // đang upload/xử lý -> ẩn nút "Phân tích & Upload"
    _uploadSid: null,         // sid của phiên upload đang chạy

    // Download
    listings: [],
    listingsLoading: false,
    downloadDest: 'zip',      // giữ để tương thích, không dùng
    currentPage: 1,
    itemsPerPage: 5,

    // Wizard
    showWizard: false,
    wizard: {
      name: '', outputDir: '', model: 'classic', address: '',
      cloudStyle: 'original', skyReplacement: true,
      perspCorrection: true, grassReplacement: false, declutter: false,
    },

    // Brackets
    showBrackets: false,
    brackets: [],
    bracketMode: 'filename',
    bracketSize: 3,
    bracketLoading: false,
    dragItem: null,
    dragOverGi: null,

    // Payment
    showPayment: false,
    paymentMemo: '',

    // Sessions
    activeSessions: 0,

    // Logs
    globalLogs: [],
    logTabs: [],

    // ── Updates (no-op trên web) ──
    async checkForUpdates() { /* web không tự cập nhật */ },
    async performUpdate() { /* no-op */ },

    // ── Init ──
    async init() {
      this.checkingSession = true;
      // Hiện lỗi đăng nhập Google (redirect kèm ?login_error=...) rồi dọn URL
      try {
        const params = new URLSearchParams(window.location.search);
        const le = params.get('login_error');
        if (le) {
          this.appLoginError = le;
          window.history.replaceState({}, '', window.location.pathname);
        }
      } catch (e) {}
      try {
        const loginRes = await api.get('/api/me');
        if (loginRes.ok) {
          this.isAppLoggedIn = true;
          this.appUsername = loginRes.username;
          this.setupUserSession(loginRes.role);
          this.appUserExpiry = loginRes.expire_at;
          this.appUserExpired = false;
          await this.refreshStatus();
        } else {
          this.isAppLoggedIn = false;
          if (loginRes.msg) this.appLoginError = loginRes.msg;
        }
      } catch (e) {
        this.appLoginError = `Lỗi khởi động: ${e}`;
      } finally {
        this.checkingSession = false;
      }

      // Kiểm tra phiên định kỳ 5 phút
      setInterval(async () => {
        if (!this.isAppLoggedIn) return;
        try {
          const loginRes = await api.get('/api/me');
          if (!loginRes.ok) {
            await this.logoutFromApp();
            this.appLoginError = loginRes.msg || 'Phiên đăng nhập đã hết hạn!';
            this.logGlobal(`⚠️ Tự động đăng xuất: ${this.appLoginError}`);
          } else if (loginRes.role !== this.appUserRole) {
            await this.logoutFromApp();
            this.appLoginError = 'Quyền tài khoản đã thay đổi. Vui lòng đăng nhập lại!';
          }
        } catch (e) { /* lỗi mạng tạm thời */ }
      }, 5 * 60 * 1000);
    },

    async loginToApp() {
      if (!this.loginInputUsername.trim() || !this.loginInputPassword.trim()) {
        this.appLoginError = 'Vui lòng nhập tài khoản và mật khẩu!';
        return;
      }
      this.appLoginLoading = true;
      this.appLoginError = '';
      try {
        const res = await api.post('/api/login', {
          username: this.loginInputUsername, password: this.loginInputPassword,
        });
        if (res.ok) {
          this.isAppLoggedIn = true;
          this.appUsername = res.username;
          this.setupUserSession(res.role);
          this.appUserExpiry = res.expire_at;
          this.appUserExpired = false;
          this.loginInputUsername = '';
          this.loginInputPassword = '';
          await this.refreshStatus();
        } else {
          this.appLoginError = res.msg || 'Đăng nhập thất bại!';
        }
      } catch (e) {
        this.appLoginError = `Lỗi hệ thống: ${e}`;
      } finally {
        this.appLoginLoading = false;
      }
    },

    loginWithGoogle() {
      this.appLoginLoading = true;
      window.location.href = '/api/login/google';
    },

    async logoutFromApp() {
      try { await api.post('/api/logout'); } catch (e) {}
      this.isAppLoggedIn = false;
      this.appUsername = '';
      this.appUserRole = '';
      this.appUserExpiry = '';
      this.appUserExpired = false;
      this.listings = [];
      this.fotelloConnected = false;
      this.autohdrConnected = false;
    },

    // ── Service ──
    setupUserSession(role) {
      this.appUserRole = role || 'member';
      if (!this.hasAccess(this.service)) {
        const order = ['fotello', 'autohdr', 'upcase', 'autoenhance'];
        this.service = order.find(s => this.hasAccess(s)) || 'fotello';
      }
    },

    isCloud() { return this.service === 'fotello' || this.service === 'autohdr'; },

    hasAccess(svc) {
      const r = (this.appUserRole || '').toLowerCase().trim();
      const roleMap = {
        hdr: 'autohdr', autohdr: 'autohdr',
        flo: 'fotello', fotello: 'fotello',
        upcase: 'upcase', upscale: 'upcase',
        enhance: 'autoenhance', ae: 'autoenhance', autoenhance: 'autoenhance',
      };
      const tokens = r.split(/[\s,+|]+/).filter(Boolean);
      const granted = tokens.map(t => roleMap[t]).filter(Boolean);
      if (granted.length) return granted.includes(svc);
      return true;
    },

    async switchService(svc) {
      if (!this.hasAccess(svc)) return;
      this.service = svc;
      this.listings = [];
      this.currentPage = 1;
      this.logGlobal(`Chuyển sang ${svc.toUpperCase()}`);
      if (svc === 'fotello' || svc === 'autohdr') await this.refreshStatus();
    },

    // ── AutoEnhance / UpCase — stub ──
    async pickAutoenhanceDir() { this.logGlobal('AutoEnhance chưa hỗ trợ trên web.'); },
    autoenhanceLogin() { this.logGlobal('AutoEnhance chưa hỗ trợ trên web.'); },
    runAutoenhance() { this.logGlobal('⚠ AutoEnhance chưa hỗ trợ trên bản web.'); },
    async loadUpcaseStatus() { this.upcase.ready = false; },
    async loadUpcaseModels() {},
    async pickUpcaseModelDir() {},
    async pickUpcaseOutput() {},
    async installUpcase() { this.logGlobal('⚠ UpCase chưa hỗ trợ trên bản web.'); },
    runUpcase() { this.logGlobal('⚠ UpCase chưa hỗ trợ trên bản web.'); },

    async refreshStatus() {
      try {
        const fs = await api.get('/api/fotello/status');
        this.fotelloConnected = !!fs.connected;
        const as = await api.get('/api/autohdr/status');
        this.autohdrConnected = !!as.connected;
      } catch (e) {}
    },

    // ── Connection (dán token/cookie) ──
    isConnected() {
      return this.service === 'fotello' ? this.fotelloConnected : this.autohdrConnected;
    },

    startLogin() { this.openConnectModal(); },

    openConnectModal() {
      this.connectInput = '';
      this.connectError = '';
      this.showConnectModal = true;
    },

    async submitConnect() {
      const val = (this.connectInput || '').trim();
      if (!val) return;
      this.connectLoading = true;
      this.connectError = '';
      try {
        let res;
        if (this.service === 'fotello') {
          res = await api.post('/api/fotello/connect', { refresh_token: val });
        } else {
          res = await api.post('/api/autohdr/connect', { cookie: val });
        }
        if (res.ok) {
          this.showConnectModal = false;
          this.logGlobal(`✅ Kết nối ${this.service.toUpperCase()} thành công!`);
          await this.refreshStatus();
        } else {
          this.connectError = res.msg || 'Kết nối thất bại';
        }
      } catch (e) {
        this.connectError = `Lỗi: ${e}`;
      } finally {
        this.connectLoading = false;
      }
    },

    async logout() {
      await api.post(`/api/${this.service}/disconnect`);
      if (this.service === 'fotello') this.fotelloConnected = false;
      else this.autohdrConnected = false;
      this.logGlobal(`Đã ngắt kết nối ${this.service.toUpperCase()}`);
      this.listings = [];
    },

    clearChromeCache() { this.openConnectModal(); },  // giữ tương thích

    onConnectionSuccess(service) {
      this.loginLoading = false;
      this.logGlobal(`✅ Kết nối ${service.toUpperCase()} thành công!`);
      this.refreshStatus();
    },
    onConnectionFailed(reason) {
      this.loginLoading = false;
      this.logGlobal(`❌ Kết nối thất bại: ${reason}`);
    },

    // ── File selection ──
    browseFiles() {
      const inp = this.$refs.fileInput;
      if (inp) inp.click();
    },

    onFilesPicked(e) {
      const files = [...(e.target.files || [])];
      if (files.length) {
        this.selectedFiles = files;
        this._uploadId = null;
        this.wizard.name = '';
        this.logGlobal(`Đã chọn ${files.length} ảnh`);
      }
      e.target.value = '';  // cho phép chọn lại cùng file
    },

    handleDrop(e) {
      this.isDragging = false;
      const files = [...(e.dataTransfer.files || [])].filter(f => f && f.type.startsWith('image/') || /\.(cr2|cr3|nef|arw|dng|raf|tif|tiff)$/i.test(f.name));
      if (files.length > 0) {
        this.selectedFiles = files;
        this._uploadId = null;
        this.wizard.name = '';
        this.logGlobal(`Đã thả ${files.length} ảnh`);
      }
    },

    // sid: nếu có -> ghi log/tiến trình vào tab phiên (thấy ngay, không phụ thuộc SSE); else -> global
    async _ensureUploaded(sid = null) {
      const say = (m) => sid ? this.addLog(m, 'info', sid) : this.logGlobal(m);
      if (this._uploadId) return this._uploadId;
      if (!this.selectedFiles.length) throw new Error('Chưa chọn ảnh');

      const MAX_BATCH_BYTES = 15 * 1024 * 1024;   // ~15MB / lô (nhỏ để mỗi request xong nhanh, tránh timeout khi server bận)
      const MAX_BATCH_FILES = 8;
      const CHUNK_THRESHOLD = 40 * 1024 * 1024;   // file lớn hơn -> tải theo mảnh (chunk)
      const CHUNK_SIZE = 20 * 1024 * 1024;        // 20MB / mảnh
      const CONCURRENCY = 3;                      // số lô tải song song (giảm từ 5 -> 3: server/mạng không kham nổi 5 lô ~30MB cùng lúc, gây 502/524)

      const files = this.selectedFiles;
      const small = files.filter(f => (f.size || 0) <= CHUNK_THRESHOLD);
      const large = files.filter(f => (f.size || 0) > CHUNK_THRESHOLD);
      const total = files.length;
      // Sinh upload_id ngay ở client để mọi lô (kể cả lô đầu) tải SONG SONG
      // ngay từ đầu, khỏi phải đợi 1 request "mở màn" lấy upload_id từ server.
      let uploadId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID().replace(/-/g, '').slice(0, 20)
        : (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
      let done = 0;

      const MAX_RETRIES = 3;      // số lần thử lại khi 1 lô/mảnh lỗi (mạng chập chờn, timeout, server quá tải...)
      const RETRY_BASE_MS = 1500; // backoff: ~1.5s, 3s, 6s (x jitter) — đủ thời gian cho server hồi phục nếu đang quá tải

      say(`Bắt đầu tải lên ${total} ảnh…`);
      const bump = () => { if (sid) this.updateProgress(done, total, Math.round(done * 100 / total), sid); };

      // "Cổng" chờ chung: khi 1 lô lỗi (thường là do server đang quá tải, không phải do riêng lô đó),
      // MỌI lô khác (kể cả lô đang chờ tới lượt) cũng phải dừng lại 1 lúc trước khi bắn tiếp — tránh
      // tình trạng cả cụm lô đồng loạt retry cùng lúc, dội thêm 1 lần quá tải nữa vào server đang hồi phục.
      const gate = { blockedUntil: 0 };
      const waitForGate = async () => {
        const wait = gate.blockedUntil - Date.now();
        if (wait > 0) await new Promise(res => setTimeout(res, wait));
      };

      // Thử lại tự động cho 1 request tải lên: an toàn để retry vì upload_id
      // cố định từ đầu -> gửi lại chỉ ghi đè đúng file cũ, không trùng/lặp.
      const withRetry = async (label, fn) => {
        let lastErr;
        for (let attempt = 1; attempt <= MAX_RETRIES + 1; attempt++) {
          await waitForGate();
          try {
            return await fn();
          } catch (e) {
            lastErr = e;
            if (attempt <= MAX_RETRIES) {
              const jitter = 0.7 + Math.random() * 0.6; // 0.7x–1.3x, tránh các lô retry đúng cùng 1 thời điểm
              const delay = Math.round(RETRY_BASE_MS * (2 ** (attempt - 1)) * jitter);
              gate.blockedUntil = Math.max(gate.blockedUntil, Date.now() + delay);
              say(`⚠ ${label} lỗi (${e.message || e}) — thử lại lần ${attempt}/${MAX_RETRIES} sau ${(delay / 1000).toFixed(1)}s…`);
              await waitForGate();
            }
          }
        }
        say(`✖ ${label} thất bại sau ${MAX_RETRIES} lần thử lại.`);
        throw lastErr;
      };

      // 1) File nhỏ: chia thành các lô ~30MB
      const batches = [];
      let batch = [], bytes = 0;
      for (const f of small) {
        if (batch.length && (bytes + (f.size || 0) > MAX_BATCH_BYTES || batch.length >= MAX_BATCH_FILES)) {
          batches.push(batch); batch = []; bytes = 0;
        }
        batch.push(f); bytes += (f.size || 0);
      }
      if (batch.length) batches.push(batch);

      const uploadBatch = async (grp, idx) => withRetry(`Lô ${idx + 1}/${batches.length}`, async () => {
        const fd = new FormData();
        if (uploadId) fd.append('upload_id', uploadId);
        grp.forEach(f => fd.append('files', f, f.name));
        const r = await fetch('/api/upload-files', { method: 'POST', credentials: 'same-origin', body: fd });
        const res = await safeJson(r);
        if (!res.ok) throw new Error(res.msg || 'Tải ảnh lên thất bại');
        uploadId = res.upload_id;
        done += grp.length; say(`⬆ Đã tải lên ${done}/${total} ảnh…`); bump();
      });

      try {
        if (batches.length) {
          // upload_id đã có sẵn (sinh ở client) -> mọi lô chạy SONG SONG ngay từ đầu (pool CONCURRENCY).
          let next = 0;
          const worker = async () => { while (next < batches.length) { const i = next++; await uploadBatch(batches[i], i); } };
          await Promise.all(Array.from({ length: Math.min(CONCURRENCY, batches.length) }, worker));
        }

        // 2) File lớn: cắt mảnh (vượt giới hạn 100MB/request của Cloudflare)
        for (const f of large) {
          const nChunks = Math.ceil(f.size / CHUNK_SIZE);
          say(`✂ ${f.name} (${(f.size / 1048576).toFixed(0)}MB) → tải ${nChunks} mảnh…`);
          for (let i = 0; i < nChunks; i++) {
            await withRetry(`${f.name} mảnh ${i + 1}/${nChunks}`, async () => {
              const fd = new FormData();
              if (uploadId) fd.append('upload_id', uploadId);
              fd.append('filename', f.name);
              fd.append('chunk_index', i);
              fd.append('total_chunks', nChunks);
              fd.append('chunk', f.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE), 'chunk');
              const r = await fetch('/api/upload-chunk', { method: 'POST', credentials: 'same-origin', body: fd });
              const res = await safeJson(r);
              if (!res.ok) throw new Error(res.msg || `Tải mảnh ${i + 1}/${nChunks} của ${f.name} thất bại`);
              uploadId = res.upload_id;
            });
            say(`⬆ ${f.name}: mảnh ${i + 1}/${nChunks}`);
          }
          done += 1; bump();
        }
      } catch (err) {
        // Retry hết mà vẫn lỗi -> dữ liệu đã tải lên dở dang không dùng được, xóa sạch
        // để tránh rác/trộn lẫn, bắt người dùng upload lại từ đầu cho chắc chắn đủ ảnh.
        say(`✖ Upload thất bại, đang xóa dữ liệu tải lên dở dang…`);
        try {
          const fd = new FormData();
          fd.append('upload_id', uploadId);
          await fetch('/api/upload-discard', { method: 'POST', credentials: 'same-origin', body: fd });
        } catch (_) { /* best-effort, bỏ qua nếu xóa cũng lỗi */ }
        this._uploadId = null;
        say(`Vui lòng tải ảnh lên lại.`);
        throw err;
      }

      this._uploadId = uploadId;
      return uploadId;
    },

    // ── Upload flow ──
    openWizard() {
      if (this.selectedFiles.length === 0) return;
      this.showWizard = true;
    },
    pickWizardDir() { /* web: không chọn thư mục */ },

    async confirmWizard() {
      if (!this.wizard.name.trim()) { this.logGlobal('⚠ Vui lòng nhập tên project'); return; }
      this.showWizard = false;

      if (this.service === 'fotello') {
        try {
          this.logGlobal('Đang tải ảnh lên máy chủ…');
          const uploadId = await this._ensureUploaded();
          this.logGlobal('Đang phân tích brackets…');
          const res = await api.post('/api/fotello/analyze-brackets', {
            upload_id: uploadId, bracket_size: this.bracketSize, mode: this.bracketMode,
          });
          if (res.ok) { this.brackets = res.brackets; this.showBrackets = true; }
          else this.logGlobal(`❌ ${res.msg}`);
        } catch (e) {
          this.logGlobal(`❌ ${e.message || e}`);
        }
      } else {
        this._runAutohdrUpload();
      }
    },

    async setBracketMode(mode) {
      if (this.bracketLoading) return;
      this.bracketMode = mode;
      this.bracketLoading = true;
      try {
        const uploadId = await this._ensureUploaded();
        const res = await api.post('/api/fotello/analyze-brackets', {
          upload_id: uploadId, bracket_size: this.bracketSize, mode,
        });
        if (res.ok) this.brackets = res.brackets;
        else this.logGlobal(`❌ ${res.msg}`);
      } catch (e) {
        this.logGlobal(`❌ Lỗi phân tích lại: ${e.message || e}`);
      } finally {
        this.bracketLoading = false;
      }
    },

    // Bracket drag-drop (giữ nguyên)
    onBracketDragStart(gi, fi) { this.dragItem = { gi, fi }; },
    onBracketDragEnd() { this.dragItem = null; this.dragOverGi = null; },
    dropToBracket(targetGi) {
      this.dragOverGi = null;
      if (!this.dragItem) return;
      const { gi, fi } = this.dragItem;
      if (gi === targetGi) { this.dragItem = null; return; }
      const file = this.brackets[gi].splice(fi, 1)[0];
      if (file === undefined) { this.dragItem = null; return; }
      this.brackets[targetGi].push(file);
      this.brackets = this.brackets.filter(g => g.length > 0);
      this.dragItem = null;
    },
    dropToNewBracket() {
      this.dragOverGi = null;
      if (!this.dragItem) return;
      const { gi, fi } = this.dragItem;
      const file = this.brackets[gi].splice(fi, 1)[0];
      if (file === undefined) { this.dragItem = null; return; }
      this.brackets.push([file]);
      this.brackets = this.brackets.filter(g => g.length > 0);
      this.dragItem = null;
    },

    async startUpload() {
      this.showBrackets = false;
      await this._runFotelloUpload();
    },

    async _runFotelloUpload() {
      const sid = this._makeSessionId();
      this._createLogTab(sid, `Upload #${sid}`);
      this._subscribe(sid);
      this.uploadRunning = true;
      this._uploadSid = sid;
      try {
        const uploadId = await this._ensureUploaded(sid);
        const res = await api.post('/api/fotello/upload', {
          upload_id: uploadId,
          prefs: {
            project_name: this.wizard.name,
            cloud_style: this.wizard.cloudStyle,
            sky_replacement: this.wizard.skyReplacement,
            perspective_correction: this.wizard.perspCorrection,
          },
          brackets: this.brackets,
          sid,
        });
        if (!res.ok) { this.addLog(`❌ ${res.msg}`, 'error', sid); this._finishUpload(sid); }
      } catch (e) {
        this.addLog(`❌ ${e.message || e}`, 'error', sid);
        this._finishUpload(sid);
      }
    },

    async _runAutohdrUpload() {
      const sid = this._makeSessionId();
      this._createLogTab(sid, `Upload #${sid}`);
      this._subscribe(sid);
      this.uploadRunning = true;
      this._uploadSid = sid;
      try {
        const uploadId = await this._ensureUploaded(sid);
        const res = await api.post('/api/autohdr/upload', {
          upload_id: uploadId,
          address: this.wizard.address || this.wizard.name,
          options: {
            model: this.wizard.model,
            sky_replacement: this.wizard.skyReplacement,
            perspective_correction: this.wizard.perspCorrection,
            grass_replacement: this.wizard.grassReplacement,
            declutter: this.wizard.declutter,
          },
          sid,
        });
        if (!res.ok) { this.addLog(`❌ ${res.msg}`, 'error', sid); this._finishUpload(sid); }
      } catch (e) {
        this.addLog(`❌ ${e.message || e}`, 'error', sid);
        this._finishUpload(sid);
      }
    },

    // Kết thúc phiên upload (lỗi/không ok ngay) -> hiện lại nút
    _finishUpload(sid) {
      if (this._uploadSid === sid) { this.uploadRunning = false; this._uploadSid = null; }
    },

    openPayment() {
      this.paymentMemo = `AUTOHDR ${this.hwid ? this.hwid.slice(0, 8).toUpperCase() : 'LICENSE'}`;
      this.showPayment = true;
    },
    copyMemo() { navigator.clipboard.writeText(this.paymentMemo); },

    async stopAll() {
      // Hiện lại nút ngay khi user bấm dừng (không chờ SSE 'done').
      this.uploadRunning = false;
      this._uploadSid = null;
      await api.post('/api/stop');
      this.logGlobal('⏹ Đã gửi lệnh dừng');
    },

    // ── Download ──
    async refreshListings() {
      this.currentPage = 1;
      this.listingsLoading = true;
      this.listings = [];
      try {
        const res = await api.get(`/api/${this.service}/listings`);
        this.listings = (res.listings || []).map(l => ({ ...l, selected: false }));
        if (!res.ok && res.msg) this.logGlobal(`⚠ ${res.msg}`);
      } catch (e) {
        this.logGlobal(`❌ ${e}`);
      }
      this.listingsLoading = false;
    },

    totalPages() { return Math.ceil(this.listings.length / this.itemsPerPage) || 1; },
    paginatedListings() {
      const start = (this.currentPage - 1) * this.itemsPerPage;
      return this.listings.slice(start, start + this.itemsPerPage);
    },
    get isAllSelected() {
      const pageItems = this.paginatedListings().filter(l => (l.enhance_count || l.photo_count || 0) > 0);
      if (pageItems.length === 0) return false;
      return pageItems.every(l => l.selected);
    },
    set isAllSelected(val) {
      this.paginatedListings().forEach(l => {
        const count = l.enhance_count || l.photo_count || 0;
        l.selected = count > 0 ? val : false;
      });
    },

    pickDownloadDest() { /* web: không chọn thư mục */ },

    async downloadSelected() {
      const selected = this.listings.filter(l => l.selected);
      if (!selected.length) return;
      const sid = this._makeSessionId();
      this._createLogTab(sid, `DL #${sid}`);
      this._subscribe(sid);
      const projects = selected.map(l => ({ id: String(l.id), name: l.name || l.uuid || String(l.id) }));
      const res = await api.post(`/api/${this.service}/download`, { projects, sid });
      if (!res.ok) this.addLog(`❌ ${res.msg}`, 'error', sid);
    },

    openFolder() { /* web: không mở thư mục local */ },

    // ── SSE ──
    _subscribe(sid) {
      try {
        const es = new EventSource('/api/events/' + sid);
        const tab = this._getTab(sid);
        if (tab) tab._es = es;
        es.onmessage = (e) => {
          let m;
          try { m = JSON.parse(e.data); } catch { return; }
          if (m.type === 'log') this.addLog(m.msg, 'info', sid);
          else if (m.type === 'progress') this.updateProgress(m.current, m.total, m.pct, sid);
          else if (m.type === 'download_ready') this._onDownloadReady(sid, m);
          else if (m.type === 'done') { es.close(); this._onTaskDone(sid); }
        };
        es.onerror = () => { /* trình duyệt tự retry; đóng khi done */ };
      } catch (e) {
        this.addLog(`❌ Lỗi SSE: ${e}`, 'error', sid);
      }
    },

    _onDownloadReady(sid, m) {
      const tab = this._getTab(sid);
      if (tab) {
        tab.downloads = tab.downloads || [];
        tab.downloads.push({ name: m.name, url: m.url });
      }
      // Tự động kích hoạt tải xuống từ trình duyệt
      try {
        const a = document.createElement('a');
        a.href = m.url;
        a.download = m.name;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => a.remove(), 1000);
      } catch (e) {}
    },

    _onTaskDone(sid) {
      const tab = this._getTab(sid);
      if (tab && !tab.done) {
        tab.done = true;
        this.activeSessions = Math.max(0, this.activeSessions - 1);
      }
      this._finishUpload(sid);   // xong (hoặc lỗi) -> hiện lại nút "Phân tích & Upload"
    },

    // ── Logs ──
    // Tự cuộn xuống cuối khi có log mới, NHƯNG nếu người dùng đã kéo lên xem
    // (cách đáy > 60px) thì không giật xuống — cho phép đọc log cũ thoải mái.
    _scrollLog(force = false) {
      const el = document.querySelector('.log-body');
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      if (force || nearBottom) {
        this.$nextTick(() => { el.scrollTop = el.scrollHeight; });
      }
    },

    logGlobal(msg) {
      this.globalLogs.push(msg);
      if (this.globalLogs.length > 500) this.globalLogs.shift();
      if (this.activeLogTab === 'global') this._scrollLog();
    },
    _makeSessionId() { return Math.random().toString(36).slice(2, 10); },
    _createLogTab(id, label) {
      const tab = { id, label, logs: [], progress: 0, folder: null, downloads: [], done: false, _es: null };
      this.logTabs.push(tab);
      this.activeLogTab = id;
      this.activeSessions++;
      this._scrollLog(true);
      return tab;
    },
    _getTab(id) { return this.logTabs.find(t => t.id === id); },
    closeLogTab(id) {
      const tab = this._getTab(id);
      if (tab) {
        if (tab._es) { try { tab._es.close(); } catch (e) {} }
        if (!tab.done) this.activeSessions = Math.max(0, this.activeSessions - 1);
      }
      this.logTabs = this.logTabs.filter(t => t.id !== id);
      if (this.activeLogTab === id) this.activeLogTab = 'global';
    },
    copyLog(id) {
      const tab = this._getTab(id);
      if (tab) navigator.clipboard.writeText(tab.logs.join('\n'));
    },

    // ── callbacks (SSE dispatch) ──
    addLog(msg, type = 'info', sessionId = 'global') {
      if (sessionId === 'global') { this.logGlobal(msg); return; }
      const tab = this._getTab(sessionId);
      if (tab) {
        tab.logs.push(msg);
        if (tab.logs.length > 500) tab.logs.shift();
        if (sessionId === this.activeLogTab) this._scrollLog();
      }
    },
    updateProgress(current, total, pct, sessionId = 'global') {
      const tab = this._getTab(sessionId);
      if (tab) tab.progress = pct;
    },
    showFolderBtn() { /* không dùng trên web */ },
  };
}

window.app = app;
