# Browser download acceleration

The bridge opens extra localhost ports so the browser companion can fetch more pages at once. This page explains how that works, what the numbers on the dashboard mean, and what to do when the browser, not the bridge, becomes the limit.

## Why ports, and not more sockets per page

Browsers cap HTTP/1.1 connections per origin at about six. The port is part of the origin, so `http://127.0.0.1:62643` and `http://127.0.0.1:62644` are different origins as far as the browser is concerned. Each extra port therefore brings six more connections to the same bridge.

The bridge calls these ports the fetch proxy. With the default of 48 ports that is up to 288 additional connection lanes for the page downloader:

```
48 ports x 6 connections = 288 extra lanes
```

Each port proxies to the same upstream CDN. The pages themselves do not change, and nothing needs to be installed for this to work.

## What the dashboard shows

The status card on the home page has a `Browser acceleration` row. When ports are open it reads, for example, `48 ports open (62643–62690) · up to 288 extra browser lanes`. When the count is zero it says `off`.

The same information is in `/health` as `fetchProxyPorts`, and the bridge prints a line at startup:

```
fetch proxy: 48 extra port(s) 62643-62690  ->  288 browser sockets for the downloader
```

To change the number, use `Faster downloads in the browser` in the dashboard's Preferences, or set `MOKURO_BRIDGE_FETCH_PORTS` in the environment. Both are read when the process starts, so restart the bridge afterwards. The value can be from 0 to 256. Setting it to 0 turns the accelerator off.

## The browser's own limit

Extra origins raise the per-origin limit. They do not raise the browser's global connection pool, which every origin shares. In Chromium, the published guidance is around 256 total pooled HTTP connections per browser, with six per host. That is why opening much more than about 42 bridge ports rarely adds real parallelism in Chromium: the global pool becomes the ceiling before the extra origins do.

Those limits are baked into Chromium. There is no supported command line switch, enterprise policy, or extension that reliably raises them, and unsupported flags such as `--max-sockets-per-group` are not a stable answer. Do not build a workflow around one.

If downloads stall while the port count is high, the useful moves are:

- Close other tabs and heavy sites, or use a dedicated browser profile for downloads, so the global pool belongs to the bridge.
- Keep `MOKURO_BRIDGE_FETCH_CONCURRENCY` modest if you set it. It is environment only and defaults to no cap; a cap is what stops many lanes from competing with each other.
- Prefer Chromium's HTTP/2 upstream paths where the CDN offers them, because one HTTP/2 connection carries many streams.
- Run the browser with a generous file descriptor limit if you are on macOS or Linux and deliberately run hundreds of connections. The bridge raises its own limit at startup; the browser process has its own.

## Firefox

Firefox exposes the relevant preferences, so it can go higher than Chromium when configured. In `about:config`:

- `network.http.max-connections` (default 900) is the global connection budget.
- `network.http.max-persistent-connections-per-server` (default 6) is the per-origin limit.
- `network.http.max-connections-per-server` caps connections to one server.

Raising `network.http.max-connections` helps only if the per-server values are also raised deliberately. Do not set them to extremes; the CDN, the bridge and the operating system all have to keep up.

## Why the dashboard suggests staying near 42 to 48

48 ports is slightly above the 256 divided by 6 ceiling, which leaves room for the main page's own connections while still using the pool fully. Higher values are allowed and harmless, but in Chromium they mostly reserve unused origins. If you use Firefox with raised limits, or you run several browsers, higher port counts can be genuinely useful.

## Related

- `docs/dashboard.md` documents the settings page and the acceleration row.
- `mokuro_bridge/fetchproxy.py` is the implementation, including the port binding and upstream matching.
- Chromium's published connection limits are in the Chrome Enterprise networking documentation: https://support.google.com/chrome/a/answer/3339263
- The global WebSocket ceiling is documented in this Chromium issue: https://issues.chromium.org/41140770
