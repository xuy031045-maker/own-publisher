/* Runs only in the creator page. It reports hashes, never image bytes or tokens. */
(() => {
  'use strict';
  if (location.origin !== 'https://creator.xiaohongshu.com') return;
  const reportName = __OWN_PUBLISHER_BINDING__;
  const xhrMetadata = new WeakMap();
  const sourceHashes = new WeakMap();
  const pixelHashes = new WeakMap();
  const groups = new Map();
  let sources = Promise.resolve([]);
  let digestTail = Promise.resolve();

  const hex = bytes => [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, '0')).join('');
  function bounded(work) {
    const next = digestTail.then(work);
    digestTail = next.catch(() => {});
    return next;
  }
  const sha256 = blob => bounded(async () => hex(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())));
  function sourceHash(blob) {
    if (!sourceHashes.has(blob)) sourceHashes.set(blob, sha256(blob));
    return sourceHashes.get(blob);
  }
  function pixelHash(blob) {
    if (!pixelHashes.has(blob)) pixelHashes.set(blob, bounded(async () => {
      const bitmap = await createImageBitmap(blob, {imageOrientation: 'from-image'});
      try {
        if (!bitmap.width || !bitmap.height || bitmap.width * bitmap.height > 40000000) return null;
        const canvas = document.createElement('canvas');
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        const context = canvas.getContext('2d', {willReadFrequently: true});
        if (!context) return null;
        context.drawImage(bitmap, 0, 0);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
        return `${canvas.width}x${canvas.height}:` + hex(await crypto.subtle.digest('SHA-256', pixels.data.buffer));
      } finally { bitmap.close(); }
    }));
    return pixelHashes.get(blob);
  }
  document.addEventListener('change', event => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || target.type !== 'file' || !target.files) return;
    const files = [...target.files];
    sources = (async () => {
      const snapshot = [];
      for (const file of files) snapshot.push({file, sha: await sourceHash(file)});
      return snapshot;
    })();
  }, true);

  function uploadUrl(raw) {
    try {
      const url = new URL(raw, location.href);
      const trusted = url.hostname === 'ros-upload.xiaohongshu.com' || /^ros-upload-d\d{1,2}\.xhscdn\.com$/.test(url.hostname);
      if (url.protocol !== 'https:' || !trusted || (url.port && url.port !== '443')) return null;
      return url;
    } catch { return null; }
  }
  async function inspect(blob, details) {
    if (!(blob instanceof Blob)) return;
    const [uploadSha, originals] = await Promise.all([sha256(blob), sources]);
    let matches = [...new Set(originals.filter(item => item.sha === uploadSha).map(item => item.sha))];
    if (!matches.length) {
      const fingerprint = await pixelHash(blob);
      if (!fingerprint) return;
      const comparisons = await Promise.all(originals.map(async item => ({sha: item.sha, equal: await pixelHash(item.file) === fingerprint})));
      matches = [...new Set(comparisons.filter(item => item.equal).map(item => item.sha))];
    }
    if (matches.length !== 1 || typeof window[reportName] !== 'function') return;
    await window[reportName]({...details, upload_sha: uploadSha, source_sha: matches[0]});
  }
  function completeParts(body) {
    if (typeof body !== 'string') return [];
    const doc = new DOMParser().parseFromString(body, 'application/xml');
    if (doc.documentElement.localName !== 'CompleteMultipartUpload') return [];
    return [...doc.getElementsByTagNameNS('*', 'PartNumber')].map(node => Number(node.textContent));
  }
  function finishGroup(group) {
    if (!group.manifest || group.sent) return;
    const numbers = group.manifest;
    if (!numbers.length || numbers.some((number, index) => number !== index + 1)) return;
    const items = numbers.map(number => group.parts.get(number));
    if (items.some(item => !item || !item.ok)) return;
    group.sent = true;
    void inspect(new Blob(items.map(item => item.blob)), {mode: 'multipart', id: group.id,
      upload_id: group.uploadId, parts: numbers}).catch(() => {});
  }
  function observeXhr(xhr, metadata, body) {
    const url = uploadUrl(metadata.url);
    if (!url) return;
    const id = decodeURIComponent(url.pathname.replace(/^\//, ''));
    if (!id) return;
    if (metadata.method === 'PUT' && !url.search && body instanceof Blob) {
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) void inspect(body, {mode: 'direct', id}).catch(() => {});
      }, {once: true});
      return;
    }
    const uploadId = url.searchParams.get('uploadId');
    if (!uploadId) return;
    const key = id + '\u0000' + uploadId;
    const group = groups.get(key) || {id, uploadId, parts: new Map(), manifest: null, sent: false};
    groups.set(key, group);
    if (metadata.method === 'PUT' && body instanceof Blob) {
      const number = Number(url.searchParams.get('partNumber'));
      if (!Number.isSafeInteger(number) || number < 1 || number > 10000) return;
      const part = {blob: body, ok: false};
      group.parts.set(number, part);
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300 && group.parts.get(number) === part) {
          part.ok = true;
          finishGroup(group);
        }
      }, {once: true});
    } else if (metadata.method === 'POST') {
      const manifest = completeParts(body);
      xhr.addEventListener('load', () => {
        try {
          const result = new DOMParser().parseFromString(xhr.responseText, 'application/xml');
          if (xhr.status >= 200 && xhr.status < 300 && result.documentElement.localName === 'CompleteMultipartUploadResult') {
            group.manifest = manifest;
            finishGroup(group);
          }
        } catch { /* Missing response evidence must fail closed. */ }
      }, {once: true});
    }
  }
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    xhrMetadata.set(this, {method: String(method).toUpperCase(), url: String(url)});
    return Reflect.apply(originalOpen, this, [method, url, ...rest]);
  };
  XMLHttpRequest.prototype.send = function(body) {
    try { observeXhr(this, xhrMetadata.get(this) || {}, body); } catch { /* Never change the upload itself. */ }
    return Reflect.apply(originalSend, this, arguments);
  };
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    const result = Reflect.apply(originalFetch, this, arguments);
    try {
      const url = uploadUrl(typeof input === 'string' || input instanceof URL ? String(input) : input.url);
      if (url && !url.search && String(init?.method || 'GET').toUpperCase() === 'PUT' && init?.body instanceof Blob) {
        const id = decodeURIComponent(url.pathname.replace(/^\//, ''));
        result.then(response => {
          if (response.ok) void inspect(init.body, {mode: 'direct', id}).catch(() => {});
        }, () => {});
      }
    } catch { /* Never change the upload itself. */ }
    return result;
  };
})();
