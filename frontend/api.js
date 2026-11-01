// バックエンド API の呼び出し。
async function req(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${method} ${url} → ${r.status}`);
  return r.json();
}

const qs = o => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString();

export const api = {
  library: (p) => req('GET', `/api/library?${qs(p)}`),
  folders: () => req('GET', '/api/library/folders'),
  facets: (p) => req('GET', `/api/library/facets?${qs(p)}`),
  scanStatus: () => req('GET', '/api/library/scan'),
  rescan: () => req('POST', '/api/library/rescan'),
  getQueue: (name) => req('GET', `/api/queue?${qs({ name })}`),
  putQueue: (items, name) => req('PUT', `/api/queue?${qs({ name })}`, { items }),
  queues: () => req('GET', '/api/queues'),
  analyze: (id, priority = 2, force = false) => req('POST', `/api/tracks/${id}/analyze?${qs({ priority, force })}`),
  analysisStatus: () => req('GET', '/api/analysis/status'),
  tracks: (ids) => req('GET', `/api/tracks?ids=${ids.join(',')}`),
  analysis: (id) => req('GET', `/api/tracks/${id}/analysis`),
  patchEdits: (id, body) => req('PATCH', `/api/tracks/${id}/edits`, body),
  sections: (id) => req('GET', `/api/tracks/${id}/sections`),
  cutpoints: (id) => req('GET', `/api/tracks/${id}/cutpoints`),
  wave: async (id) => {
    const r = await fetch(`/api/tracks/${id}/wave`);
    if (!r.ok) throw new Error(`wave ${id} → ${r.status}`);
    return new Uint8Array(await r.arrayBuffer());
  },
};
