import { api } from './api.js';
import assets from '../build/assets.js';
export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path.startsWith('/api/') || path.startsWith('/media/')) return api(request, env);
    if (!['GET', 'HEAD'].includes(request.method))
      return new Response('Method not allowed', { status: 405 });
    const item =
      assets[path] || (!path.split('/').at(-1).includes('.') ? assets['/index.html'] : null);
    if (!item) return new Response('Not found', { status: 404 });
    const bytes = Uint8Array.from(atob(item.body), (c) => c.charCodeAt(0));
    return new Response(request.method === 'HEAD' ? null : bytes, {
      headers: {
        'content-type': item.type,
        'cache-control': path.startsWith('/assets/')
          ? 'public,max-age=31536000,immutable'
          : 'no-cache',
        'x-content-type-options': 'nosniff',
        'referrer-policy': 'strict-origin-when-cross-origin',
        'content-security-policy':
          "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
      },
    });
  },
};
