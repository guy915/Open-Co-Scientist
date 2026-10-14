function assetRequest(request, pathname) {
  const url = new URL(request.url);
  url.pathname = pathname;
  return new Request(url, request);
}

async function asset(pathname, request, env) {
  return env.ASSETS.fetch(assetRequest(request, pathname));
}

function noIndex(response, status = response.status) {
  const result = new Response(response.body, {
    status,
    statusText: response.statusText,
    headers: response.headers,
  });
  result.headers.set('X-Robots-Tag', 'noindex, nofollow');
  return result;
}

async function notFound(request, env) {
  return noIndex(await asset('/404.html', request, env), 404);
}

async function privatePage(request, env) {
  const response = await asset('/private.html', request, env);
  return response.status === 404 ? notFound(request, env) : noIndex(response);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const {pathname} = url;

    if (pathname.startsWith('/assets/')) {
      const response = await env.ASSETS.fetch(request);
      if (response.status !== 404) return response;
      const missing = new Response(response.body, response);
      missing.headers.set('Cache-Control', 'no-store');
      return missing;
    }

    if (pathname === '/' || pathname === '/index.html') {
      const response = await asset('/index.html', request, env);
      return response.status === 404 ? notFound(request, env) : response;
    }

    if (/^\/privacy(?:\/index\.html|\/)?$/.test(pathname)) {
      const response = await asset('/privacy/index.html', request, env);
      return response.status === 404 ? notFound(request, env) : response;
    }

    const example = pathname.match(
      /^\/examples\/([A-Za-z0-9_-]+)(?:\/index\.html|\/)?$/,
    );
    if (example) {
      const response = await asset(
        `/examples/${example[1]}/index.html`,
        request,
        env,
      );
      return response.status === 404 ? notFound(request, env) : response;
    }

    if (/^\/(?:chats|runs|operations)(?:\/|$)/.test(pathname)) {
      return privatePage(request, env);
    }

    if (pathname === '/private.html' || pathname === '/404.html') {
      return notFound(request, env);
    }

    const response = await env.ASSETS.fetch(request);
    if (response.status !== 404) return response;
    return notFound(request, env);
  },
};
