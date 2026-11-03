/* Shared rows use optimistic concurrency; drafts remain on this device. */
class WorkspaceSync {
  constructor({key, url, initialData, shared, onChange, protocol = 3}) {
    Object.assign(this, {key, url, shared, onChange, protocol});
    this.defaults = Object.fromEntries(Object.entries(initialData).map(([t, rows]) =>
      [t, rows.map((row, i) => ({...row, _id: row._id || t + '-' + i, _o: row._o ?? i}))]));
    this.data = this.copy(this.defaults);
    this.base = this.copy(this.defaults);
    this.versions = Object.fromEntries(shared.map(t => [t, {}]));
    this.pending = {};
    this.remote = {};
    this.tab = 'start';
    this.volRatio = 20;
    this.prefix = key + ':draft-v3:';
    this.id = this.prefix + crypto.randomUUID();
    this.active = true;
    this.requests = new Set();
  }

  copy(value) { return JSON.parse(JSON.stringify(value)); }
  equal(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  hasPending() { return Object.keys(this.pending).length > 0; }
  snapshot() {
    return {format: 3, url: this.url, data: this.data, base: this.base,
      versions: this.versions, pending: this.pending, tab: this.tab,
      volRatio: this.volRatio, updatedAt: new Date().toISOString()};
  }

  read(id) {
    const record = JSON.parse(localStorage.getItem(id) || 'null');
    if (!record || record.format !== 3 || record.url !== this.url) return null;
    if (!record.data || !record.base || !record.versions || !record.pending ||
        !Object.keys(this.defaults).every(t => Array.isArray(record.data[t]) && Array.isArray(record.base[t])) ||
        !this.shared.every(t => record.versions[t] && typeof record.versions[t] === 'object') ||
        Object.values(record.pending).some(p => !this.shared.includes(p.table) || typeof p.id !== 'string' || (!p.del && !p.row))) {
      throw new Error('Invalid local draft');
    }
    return record;
  }

  adopt(record) {
    for (const k of ['data', 'base', 'versions', 'pending']) this[k] = this.copy(record[k]);
    // Preserve operation identity when restoring a draft from before moderation.
    if (this.protocol === 4) for (const pending of Object.values(this.pending)) {
      if (pending.request?.protocol === 3) pending.request.protocol = 4;
    }
    this.tab = record.tab || 'start';
    this.volRatio = record.volRatio || 20;
  }

  start() {
    let previous, record;
    try {
      previous = sessionStorage.getItem(this.key + ':active-draft');
      record = previous && this.read(previous) || this.read(this.key + ':cache-v3');
      if (record) this.adopt(record);
      else {
        const old = JSON.parse(localStorage.getItem(this.key) || 'null');
        if (old?.tab) this.tab = old.tab;
      }
      sessionStorage.setItem(this.key + ':active-draft', this.id);
    } catch (_) { this.storageError = true; }
    if (this.persist() && previous && record && !Object.keys(record.pending).length) {
      // A durable replacement avoids accumulating a full clean snapshot on every reload.
      try { localStorage.removeItem(previous); } catch (_) { /* Keeping an extra copy is safe. */ }
    }
    this.onOnline = () => this.retry();
    this.onUnload = e => {
      if (this.hasPending() || this.storageError) { e.preventDefault(); e.returnValue = ''; }
    };
    window.addEventListener('online', this.onOnline);
    window.addEventListener('beforeunload', this.onUnload);
    this.publish();
    if (this.url) {
      this.pull();
      this.interval = setInterval(() => this.pull(), 20000);
    }
  }

  stop() {
    this.active = false;
    clearTimeout(this.sendTimer);
    clearInterval(this.interval);
    for (const request of this.requests) request.abort();
    window.removeEventListener('online', this.onOnline);
    window.removeEventListener('beforeunload', this.onUnload);
  }

  persist() {
    try {
      localStorage.setItem(this.id, JSON.stringify(this.snapshot()));
      this.storageError = false;
      return true;
    } catch (_) { this.storageError = true; return false; }
  }

  drafts() {
    const records = [];
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const id = localStorage.key(i);
        if (!id.startsWith(this.prefix) || id === this.id) continue;
        try {
          const record = this.read(id);
          if (record && Object.keys(record.pending).length) {
            records.push({id, at: new Date(record.updatedAt).toLocaleString('ru-RU'),
              tables: [...new Set(Object.values(record.pending).map(p => p.table))].join(', ')});
          }
        } catch (_) { /* Keep unreadable records untouched for recovery. */ }
      }
    } catch (_) { this.storageError = true; }
    return records;
  }

  publish() {
    if (!this.active) return;
    const conflicts = [...new Set(Object.values(this.pending).filter(p => p.conflict).map(p => p.table))];
    let sync = !this.url ? 'local' : !this.loaded ? 'loading' : 'ok';
    if (this.hasPending() && this.url) sync = this.writing ? 'saving' : 'pending';
    if (!this.writing && this.hasPending() && Object.values(this.pending).every(p => p.awaiting)) sync = 'approval';
    if (Object.values(this.pending).some(p => p.rejected)) sync = 'rejected';
    if (this.networkError) sync = 'err';
    if (this.upgrade) sync = 'upgrade';
    if (conflicts.length) sync = 'conflict';
    if (this.storageError) sync = 'storage';
    const drafts = this.drafts();
    if (this.storageError) sync = 'storage';
    this.onChange({data: this.copy(this.data), tab: this.tab, volRatio: this.volRatio,
      sync, syncAt: this.syncAt || '', conflictTables: conflicts, recoveryDrafts: drafts});
  }

  saveView(tab, volRatio) {
    this.tab = tab;
    this.volRatio = volRatio;
    this.persist();
    this.publish();
  }

  update(table, fn) {
    if (!Object.hasOwn(this.data, table)) throw new Error('Unknown table');
    const before = new Map(this.data[table].map(r => [r._id, r]));
    this.data[table] = fn(this.copy(this.data[table])).map(row => row._id ? row :
      {...row, _id: table + '-' + crypto.randomUUID(), _o: Date.now()});
    if (this.shared.includes(table)) {
      const after = new Map(this.data[table].map(r => [r._id, r]));
      for (const id of new Set([...before.keys(), ...after.keys()])) {
        const row = after.get(id) || null;
        if (this.equal(before.get(id) || null, row)) continue;
        const key = table + '|' + id;
        const pending = this.pending[key] || {table, id,
          baseVersion: this.ready ? this.versions[table][id] || 0 : null,
          baseRow: this.copy(this.base[table].find(r => r._id === id) || null)};
        pending.row = this.copy(row);
        pending.del = !row;
        if (pending.rejected) {
          delete pending.rejected; delete pending.awaiting; delete pending.request;
        }
        this.pending[key] = pending;
      }
    }
    this.persist();
    this.publish();
    this.schedule(800);
  }

  schedule(delay) {
    clearTimeout(this.sendTimer);
    if (this.active && this.url) this.sendTimer = setTimeout(() => this.flush(), delay);
  }

  async json(options) {
    const controller = new AbortController();
    this.requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(this.url, {...options, cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return await response.json();
    } finally {
      clearTimeout(timeout);
      this.requests.delete(controller);
    }
  }

  async pull() {
    if (!this.active || this.reading || !this.url) return;
    this.reading = true;
    const epoch = this.readEpoch || 0;
    try {
      const result = await this.json();
      if (!this.active || epoch !== (this.readEpoch || 0)) return;
      if (!result || !result.data || typeof result.data !== 'object' || Array.isArray(result.data)) throw new Error('Invalid data');
      const modern = result.protocol === this.protocol && result.ok === true;
      if (modern && !this.shared.every(t => {
        const tb = result.data[t];
        return !tb || tb.rows && !Array.isArray(tb.rows) && Array.isArray(tb.deleted) && tb.deletedVersions &&
          Object.entries(tb.rows).every(([id, x]) => x?.row?._id === id && Number.isSafeInteger(x.v) && x.v > 0) &&
          tb.deleted.every(id => Number.isSafeInteger(tb.deletedVersions[id]) && tb.deletedVersions[id] > 0);
      })) throw new Error('Invalid versions');
      if (!modern && (result.protocol !== undefined || result.ok === false)) throw new Error('Server error');
      this.upgrade = !modern;
      this.ready = modern;
      for (const t of this.shared) {
        if (!modern) {
          // Legacy snapshots are display-only. They can never acknowledge queued work.
          const old = result.data[t];
          let rows = Array.isArray(old) ? old.map((r, i) => ({...r, _id: r._id || t + '-' + i, _o: r._o ?? i})) : null;
          if (old?.rows && Array.isArray(old.deleted)) {
            const byId = new Map(this.defaults[t].map(r => [r._id, r]));
            for (const entry of Object.values(old.rows)) if (entry?.row?._id) byId.set(entry.row._id, entry.row);
            old.deleted.forEach(id => byId.delete(id));
            rows = [...byId.values()];
          }
          if (rows && !Object.values(this.pending).some(p => p.table === t)) {
            this.data[t] = this.copy(rows);
            this.base[t] = this.copy(rows);
          }
          continue;
        }
        const tb = result.data[t] || {rows: {}, deleted: [], deletedVersions: {}};
        const remote = new Map(this.defaults[t].map(row => [row._id, {row, version: 0}]));
        for (const [id, x] of Object.entries(tb.rows)) remote.set(id, {row: x.row, version: x.v});
        for (const id of tb.deleted) remote.set(id, {row: null, version: tb.deletedVersions[id]});
        const ids = new Set([...remote.keys(), ...this.data[t].map(r => r._id), ...Object.keys(this.versions[t])]);
        for (const id of ids) {
          const latest = remote.get(id) || {row: null, version: 0};
          if (latest.version < (this.versions[t][id] || 0)) continue;
          const key = t + '|' + id;
          this.remote[key] = this.copy(latest);
          const pending = this.pending[key];
          if (pending) {
            // Retry uncertain writes with their original mutation ID before accepting a newer read.
            if (!pending.request) {
              if (pending.baseVersion === null && this.equal(pending.baseRow, latest.row)) pending.baseVersion = latest.version;
              if (pending.baseVersion !== latest.version || !this.equal(pending.baseRow, latest.row)) pending.conflict = true;
            }
            continue;
          }
          // Keep row order and focus stable while the user is inside an editor.
          const focused = typeof document !== 'undefined' && document.activeElement;
          if (focused?.dataset?.t === t || t === 'briefs' && focused?.dataset?.k) continue;
          this.data[t] = this.withRow(this.data[t], id, latest.row);
          this.base[t] = this.withRow(this.base[t], id, latest.row);
          this.versions[t][id] = latest.version;
        }
      }
      this.loaded = true;
      this.networkError = false;
      this.persist();
      // Cache only acknowledged tables. Pending changes live in separate per-page draft records.
      const cache = {...this.snapshot(), data: {...this.base, calc: this.data.calc}, pending: {}};
      try { localStorage.setItem(this.key + ':cache-v3', JSON.stringify(cache)); }
      catch (_) { this.storageError = true; }
      this.schedule(0);
    } catch (_) { if (this.active && epoch === (this.readEpoch || 0)) this.networkError = true; }
    finally { if (epoch === (this.readEpoch || 0)) { this.reading = false; this.publish(); } }
  }

  withRow(rows, id, row) {
    return [...rows.filter(r => r._id !== id), ...(row ? [this.copy(row)] : [])]
      .sort((a, b) => (a._o || 0) - (b._o || 0) || a._id.localeCompare(b._id));
  }

  async flush() {
    if (!this.active || !this.ready || this.writing || this.storageError) return;
    const candidates = Object.keys(this.pending).filter(k => !this.pending[k].conflict && !this.pending[k].rejected && this.pending[k].baseVersion !== null);
    const key = candidates.find(k => !this.pending[k].awaiting) || candidates.sort((a, b) =>
      (this.pending[a].checkedAt || 0) - (this.pending[b].checkedAt || 0))[0];
    if (!key) return;
    const pending = this.pending[key], t = pending.table, id = pending.id;
    if (!pending.request) pending.request = {protocol: this.protocol, table: t, id, baseVersion: pending.baseVersion,
      row: this.copy(pending.row), del: pending.del, mutationId: crypto.randomUUID()};
    if (!this.persist()) { this.publish(); return; }
    const request = this.copy(pending.request);
    this.writing = true;
    this.publish();
    let retry = false;
    try {
      const result = await this.json({method: 'POST', headers: {'Content-Type': 'text/plain;charset=utf-8'}, body: JSON.stringify(request)});
      if (!this.active) return;
      if (result?.protocol !== this.protocol || result.table !== t || result.id !== id || result.mutationId !== request.mutationId) throw new Error('Invalid acknowledgement');
      if (this.protocol === 4 && result.status === 'pending' && result.ok === true) {
        pending.awaiting = true; pending.checkedAt = Date.now();
        this.networkError = false; this.persist(); retry = true;
        return;
      }
      if (this.protocol === 4 && result.status === 'rejected' && result.ok === false && result.error === 'rejected') {
        if (pending.del !== request.del || !this.equal(pending.row, request.row)) {
          delete pending.request; delete pending.awaiting;
        } else pending.rejected = true;
        this.networkError = false; this.persist();
        return;
      }
      if (this.protocol === 4 && !['approved', 'conflict'].includes(result.status)) throw new Error('Invalid review status');
      if (
          !Number.isSafeInteger(result.version) || typeof result.deleted !== 'boolean' ||
          (result.deleted ? result.row !== null : result.row?._id !== id)) throw new Error('Invalid acknowledgement');
      if (result.ok === false && result.error === 'conflict' && result.version > request.baseVersion) {
        pending.conflict = true;
        this.remote[key] = {row: this.copy(result.row), version: result.version};
      } else {
        if (result.ok !== true || result.version !== request.baseVersion + 1 || result.deleted !== request.del || !this.equal(result.row, request.row)) throw new Error('Save rejected');
        this.base[t] = this.withRow(this.base[t], id, result.row);
        this.versions[t][id] = result.version;
        if (pending.del === request.del && this.equal(pending.row, request.row)) delete this.pending[key];
        else {
          pending.baseVersion = result.version;
          pending.baseRow = this.copy(result.row);
          delete pending.request; delete pending.awaiting;
        }
        this.syncAt = new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
      }
      this.networkError = false;
      this.persist();
    } catch (_) { this.networkError = true; retry = true; }
    finally {
      this.writing = false;
      this.publish();
      const unsent = Object.values(this.pending).some(p => !p.awaiting && !p.conflict && !p.rejected);
      this.schedule(retry && (this.networkError || !unsent) ? 5000 : 0);
    }
  }

  retry() {
    this.persist();
    this.publish();
    this.pull();
    this.flush();
  }

  archive() {
    if (this.hasPending()) localStorage.setItem(this.prefix + crypto.randomUUID(), JSON.stringify(this.snapshot()));
  }

  restore(id) {
    if (this.writing || !id.startsWith(this.prefix)) return;
    try {
      const record = this.read(id);
      if (!record) return;
      this.archive();
      this.adopt(record);
      this.remote = {};
      this.readEpoch = (this.readEpoch || 0) + 1;
      this.reading = false;
      this.ready = false;
      this.loaded = false;
      this.persist();
      this.publish();
      this.pull();
    } catch (_) { this.storageError = true; this.publish(); }
  }

  useRemote() {
    if (this.writing) return;
    try {
      this.archive();
      for (const [key, pending] of Object.entries(this.pending)) {
        if (!pending.conflict || !this.remote[key]) continue;
        const {table: t, id} = pending, latest = this.remote[key];
        this.data[t] = this.withRow(this.data[t], id, latest.row);
        this.base[t] = this.withRow(this.base[t], id, latest.row);
        this.versions[t][id] = latest.version;
        delete this.pending[key];
      }
      this.persist();
      this.publish();
      this.schedule(0);
    } catch (_) { this.storageError = true; this.publish(); }
  }

  download() {
    const blob = new Blob([JSON.stringify({...this.snapshot(), remote: this.remote}, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'sunday-open-backup-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

globalThis.WorkspaceSync = WorkspaceSync;
