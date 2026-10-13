export default {
  async fetch(request, env) {
    const response = await env.ASSETS.fetch(request);
    const url = new URL(request.url);
    if (response.status === 404 && url.pathname.startsWith('/assets/')) {
      // _headers marks /assets/* immutable; a miss must not be cached that
      // way, or a bundle published after the miss stays unreachable.
      const missing = new Response(response.body, response);
      missing.headers.set('Cache-Control', 'no-store');
      return missing;
    }
    if (response.status !== 404) {
      return response;
    }
    url.pathname = '/index.html';
    url.search = '';
    return env.ASSETS.fetch(new Request(url, request));
  },
};
