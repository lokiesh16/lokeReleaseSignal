(function() {
  if (window.__testgenNetworkInjected) return;
  window.__testgenNetworkInjected = true;
  window.__testgenNetworkLog = [];

  // Helper to safely stringify body
  function safeStringify(data) {
    if (!data) return null;
    try {
      if (typeof data === 'string') return data;
      if (data instanceof FormData) {
        var obj = {};
        data.forEach(function(v, k) { obj[k] = typeof v === 'string' ? v : '[File]'; });
        return JSON.stringify(obj);
      }
      if (data instanceof URLSearchParams) return data.toString();
      if (data instanceof Blob) return '[Blob ' + data.size + ' bytes]';
      if (data instanceof ArrayBuffer) return '[ArrayBuffer ' + data.byteLength + ' bytes]';
      return JSON.stringify(data);
    } catch (e) {
      return '[unserializable]';
    }
  }

  // Helper to extract headers
  function headersToObj(headers) {
    var obj = {};
    if (!headers) return obj;
    try {
      if (headers.forEach) {
        headers.forEach(function(v, k) { obj[k] = v; });
      } else if (typeof headers === 'object') {
        Object.keys(headers).forEach(function(k) { obj[k] = headers[k]; });
      }
    } catch (e) {}
    return obj;
  }

  // Check if URL is an API call
  function isApiUrl(url) {
    if (!url) return false;
    // Skip static assets
    var skipPatterns = ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot', 'fonts.googleapis', 'cdn.'];
    if (skipPatterns.some(function(p) { return url.indexOf(p) > -1; })) return false;
    // API patterns
    var patterns = ['/api/', '/graphql', '/v1/', '/v2/', '/v3/', '/rest/', '.json', '/auth/', '/login', '/logout', '/users', '/data', '/query', '/mutation', '/search', '/fetch', '/get', '/post', '/put', '/delete', '/create', '/update', '/save', '/load', 'amazonaws.com', 'firebase', 'supabase', 'hasura', '/gql', '/rpc'];
    if (patterns.some(function(p) { return url.toLowerCase().indexOf(p) > -1; })) return true;
    // Check if it's likely an API by content type or XHR/fetch
    return false;
  }

  // Log network request
  function logRequest(entry) {
    window.__testgenNetworkLog.push(entry);
    if (window.__testgenNetworkLog.length > 100) window.__testgenNetworkLog.shift();
    // Notify if listener exists
    if (window.__testgenOnNetwork) {
      try { window.__testgenOnNetwork(entry); } catch(e) {}
    }
  }

  // ─── Intercept Fetch ─────────────────────────────────────────────────────────
  var origFetch = window.fetch;
  window.fetch = function(input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    var reqHeaders = headersToObj((init && init.headers) || (input && input.headers));
    var reqBody = safeStringify((init && init.body) || null);
    
    var entry = {
      type: 'fetch',
      url: url,
      method: method,
      requestHeaders: reqHeaders,
      requestBody: reqBody,
      ts: Date.now(),
      timestamp: new Date().toISOString(),
      status: null,
      statusText: null,
      responseHeaders: {},
      responseBody: null,
      duration: null,
      isApi: isApiUrl(url)
    };

    var startTime = performance.now();

    return origFetch.apply(this, arguments)
      .then(function(response) {
        entry.status = response.status;
        entry.statusText = response.statusText;
        entry.responseHeaders = headersToObj(response.headers);
        entry.duration = Math.round(performance.now() - startTime);

        // Clone response to read body without consuming it
        var clone = response.clone();
        clone.text().then(function(text) {
          entry.responseBody = text;
          logRequest(entry);
        }).catch(function() {
          logRequest(entry);
        });

        return response;
      })
      .catch(function(err) {
        entry.status = 0;
        entry.statusText = 'Network Error';
        entry.error = err.message;
        entry.duration = Math.round(performance.now() - startTime);
        logRequest(entry);
        throw err;
      });
  };

  // ─── Intercept XMLHttpRequest ────────────────────────────────────────────────
  var origXHROpen = XMLHttpRequest.prototype.open;
  var origXHRSend = XMLHttpRequest.prototype.send;
  var origXHRSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function(method, url) {
    this.__testgen = {
      method: method.toUpperCase(),
      url: url,
      requestHeaders: {},
      ts: Date.now(),
      timestamp: new Date().toISOString(),
      isApi: isApiUrl(url)
    };
    return origXHROpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    if (this.__testgen) {
      this.__testgen.requestHeaders[name] = value;
    }
    return origXHRSetHeader.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function(body) {
    var xhr = this;
    var startTime = performance.now();
    
    if (xhr.__testgen) {
      xhr.__testgen.requestBody = safeStringify(body);
      xhr.__testgen.type = 'xhr';
    }

    xhr.addEventListener('loadend', function() {
      if (!xhr.__testgen) return;
      
      var entry = xhr.__testgen;
      entry.status = xhr.status;
      entry.statusText = xhr.statusText;
      entry.duration = Math.round(performance.now() - startTime);
      
      // Parse response headers
      var headerStr = xhr.getAllResponseHeaders();
      entry.responseHeaders = {};
      if (headerStr) {
        headerStr.trim().split(/[\r\n]+/).forEach(function(line) {
          var parts = line.split(': ');
          if (parts.length === 2) entry.responseHeaders[parts[0]] = parts[1];
        });
      }
      
      // Get response body
      try {
        entry.responseBody = xhr.responseText || '';
      } catch(e) {
        entry.responseBody = '[unable to read]';
      }
      
      logRequest(entry);
    });

    return origXHRSend.apply(this, arguments);
  };

  // ─── Beacon API ──────────────────────────────────────────────────────────────
  if (navigator.sendBeacon) {
    var origBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function(url, data) {
      logRequest({
        type: 'beacon',
        url: url,
        method: 'POST',
        requestBody: safeStringify(data),
        ts: Date.now(),
        timestamp: new Date().toISOString(),
        status: null,
        isApi: isApiUrl(url)
      });
      return origBeacon(url, data);
    };
  }

  console.log('[TestGen] Network interception active - capturing API requests');
})();
