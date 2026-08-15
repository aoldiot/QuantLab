export function generateUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    return ('10000000-1000-4000-8000-100000000000').replace(/[018]/g, (c: string) => {
      const n = Number(c);
      return (n ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (n / 4)))).toString(16);
    });
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// Polyfill window.crypto.randomUUID if in insecure context (e.g. HTTP non-localhost)
if (typeof window !== 'undefined') {
  try {
    if (!window.crypto) {
      (window as unknown as { crypto: unknown }).crypto = {};
    }
    if (!window.crypto.randomUUID) {
      window.crypto.randomUUID = generateUUID as () => `${string}-${string}-${string}-${string}-${string}`;
    }
  } catch {
    // Ignore if property is non-configurable
  }
}

export function getClientId(): string {
  let value = localStorage.getItem('quantlab_client_id');
  if (!value) {
    value = generateUUID();
    localStorage.setItem('quantlab_client_id', value);
  }
  return value;
}
