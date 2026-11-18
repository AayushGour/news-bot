# SearXNG through a rotating Tor proxy: feasibility (2026-11-18)

**Root question (verbatim):** "Is it feasible to route a self-hosted SearXNG instance's outgoing search-engine requests through a rotating Tor proxy (or similar rotating egress) so that requests come from other IPs and stop getting CAPTCHA/rate-limited — and if so, what is the simplest reliable setup for our Docker Compose stack?"

Mode: deep desk research using WebSearch, WebFetch and read-only `curl` of upstream source and docs. No containers were started and no traffic went through Tor. Local facts come from `docker-compose.yml`, `searxng/settings.yml`, `src/pipeline/search.py` and `docker image inspect` (the local image is `searxng/searxng` **2026.7.13-9e25585ae**).

---

## Verdict: technically feasible, but not worth it as the fix

- **Mechanically it is easy.** One sidecar container plus about 10 lines of `settings.yml`. SearXNG rotates round-robin over a proxy list on every request natively ([docs](https://docs.searxng.org/admin/settings/settings_outgoing.html), [network.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/network/network.py)).
- **It will not reliably fix the problem.**
  - Tor exits are public and pre-flagged.
  - Google in particular CAPTCHAs them ([Tor support FAQ](https://support.torproject.org/tbb/tbb-35/), [tor-talk](https://lists.torproject.org/pipermail/tor-talk/2016-June/041267.html)).
  - Two of our five dead engines are blocked by **proof-of-work challenges, not by IP** (Startpage and Mojeek, see below), so a new IP does nothing for them.
  - SearXNG's own docs-adjacent guides say proxying "sometimes [helps], and sometimes it makes them worse" ([SSD Nodes guide](https://www.ssdnodes.com/learn/searxng-route-engine-requests-through-a-proxy)).
- **SearXNG's suspension logic defeats rotation by default.** One CAPTCHA on one exit suspends the engine for every exit for 1 hour (reCAPTCHA: 7 days). The defaults are in [settings.yml `search.suspended_times`](https://raw.githubusercontent.com/searxng/searxng/master/searx/settings.yml).
- **Recommendation:** treat it as an optional, measured experiment scoped to brave and duckduckgo only. The real fix is fewer queries plus caching, and one official API engine (Brave Search API or Exa) plugged into SearXNG for the general web (see the ranking at the end).

---

## 1. SearXNG native support (current master, checked 2026-11-18)

| Setting | What it actually does | Source |
|---|---|---|
| `outgoing.proxies` | Supports `http`, `https`, `socks4`, `socks5` and `socks5h`. With more than one proxy per pattern, requests are "distributed in a round-robin fashion". Example shape: `all://: [url1, url2]`. | [docs](https://docs.searxng.org/admin/settings/settings_outgoing.html) |
| Rotation granularity | `get_client()` calls `next(self._proxies_cycle)` on **every request**. A network-level retry calls `get_client()` again, so **a retry goes out through the next proxy**. | [network.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/network/network.py) |
| `outgoing.using_tor_proxy` | Fail-closed check. On first use of each proxy it GETs `https://check.torproject.org/api/ip`. If `IsTor` is false it raises `ProxyError('...not using Tor')`. Requires that every proxy URL starts with `socks5h://`. It also activates the onion-only engines. It does **not** set a proxy by itself. | [network.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/network/network.py), [engines/\_\_init\_\_.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/engines/__init__.py) |
| `outgoing.extra_proxy_timeout` | **Only applies to engines that define `onion_url`.** The code: `if using_tor_proxy(engine) and hasattr(engine, 'onion_url'): engine.timeout += extra_proxy_timeout`. None of brave, duckduckgo, google_cse, yahoo, bing, startpage or mojeek has an `onion_url` (checked by grep of each engine module). For our engines, raise `timeout:` per engine instead. Several third-party guides get this wrong. | [engines/\_\_init\_\_.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/engines/__init__.py) |
| Per-engine `proxies:` / `using_tor_proxy:` / `network:` | Each engine overrides the outgoing proxies. `network: <name>` reuses a named network from `outgoing.networks`, or another engine's network. Each engine otherwise gets its own network, which means its own proxy cycle. | [engines docs](https://raw.githubusercontent.com/searxng/searxng/master/docs/admin/settings/settings_engines.rst), [network.py `initialize`](https://raw.githubusercontent.com/searxng/searxng/master/searx/network/network.py) |
| `request_timeout` | Default is 3.0 s ([default settings.yml](https://raw.githubusercontent.com/searxng/searxng/master/searx/settings.yml)). Ours is already 8.0, with a max of 15.0. | local `searxng/settings.yml` |
| `search.suspended_times` | Defaults: CAPTCHA 3600 s, 429/too-many 180 s, access denied 180 s, reCAPTCHA 604800 s, Cloudflare CAPTCHA 1296000 s. **The suspension is per engine, not per proxy.** | [default settings.yml](https://raw.githubusercontent.com/searxng/searxng/master/searx/settings.yml) |
| Proxy redundancy PR | PR #3491 sent the same request in parallel through several proxies. It was built because "using SearX over unreliable proxies, like tor, seems to be quite error prone". It was closed unmerged on 2026-09-25. | [PR #3491](https://github.com/searxng/searxng/pull/3491) |

Why `retries` probably won't save a CAPTCHA: a CAPTCHA usually comes back as HTTP 200 with a challenge page, and the engine detects it in parsing *after* the network layer. A retry then only happens if `retry_on_http_error` matches a status code. **This is my inference from the code, not verified live.**

## 2. Do the engines block Tor exits? Would Tor beat our home IP?

| Engine | What we see now | Evidence on the Tor or IP angle | Would Tor help? |
|---|---|---|---|
| **google cse** | "too many requests" | Google CAPTCHAs Tor exits as standard ("unusual traffic"). The [Tor FAQ](https://support.torproject.org/tbb/tbb-35/) says sites CAPTCHA Tor because exits are shared. Long-running reports: [tor-relays](https://lists.torproject.org/pipermail/tor-relays/2014-July/004938.html), [tor-talk 403](https://lists.torproject.org/pipermail/tor-talk/2016-June/041267.html), [Tor forum 2023](https://forum.torproject.org/t/error-message-at-google-com/5322). Our `google_cse` scrapes a shared partner CX key (blackle.com), not an official API ([google_cse.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/engines/google_cse.py)). | **Likely worse.** A reCAPTCHA hit means a 7-day suspension. |
| **duckduckgo** | CAPTCHA | DDG is Tor Browser's default engine and runs an onion service ([Tor support](https://support.torproject.org/tbb/tbb-24/)), so it does not blanket-block Tor. But SearXNG's DDG CAPTCHAs are a known cross-instance problem ([#6779](https://github.com/searxng/searxng/issues/6779) says it "ends with CAPTCHA on all instances I tested" for language `all`; also [#6596](https://github.com/searxng/searxng/issues/6596) and [#4824](https://github.com/searxng/searxng/issues/4824)). Our instance sends `default_lang: en`, so the `all` trigger may not apply here (unverified). | **Maybe.** This is the best candidate. Unverified. |
| **brave** | "too many requests" | Brave runs a Search onion ([Brave community](https://community.brave.app/t/update-new-onion-addresses-for-brave-website-and-brave-search/271955)), so Tor is not banned outright. A 429 is a per-IP rate limit, which is exactly what rotation addresses. Shared exits also carry other people's load, though. | **Maybe.** Unverified. |
| **startpage** | "Suspended: CAPTCHA" | Startpage put an **Anubis proof-of-work** challenge on its search endpoint. Upstream set `inactive: true` and said "there is no way to get around this" ([PR #6669](https://api.github.com/repos/searxng/searxng/pulls/6669), merged 2026-09-10). Our image (2026.7.13) predates that change. | **No.** It is not IP-based. |
| **mojeek** | 0 results | Upstream marks it `inactive: true`, quote: "uses a Proof of Work CAPTCHA" ([default settings.yml](https://raw.githubusercontent.com/searxng/searxng/master/searx/settings.yml)). | **No.** |
| **yahoo** | HTTP error | No Tor-specific evidence found. **Unverified.** | Unknown |
| **bing** | answers, but junk | Third-party guide: "Many engines (Google, Bing, etc.) treat Tor exits as suspicious" ([gist](https://gist.github.com/RonTuretzky/a2f202dd8989547cc55a1c34ff583434)). | **Keep it direct**, since it works today. |

Overall field reports:
- Over Tor, "Wikipedia, Wikidata, GitHub and arXiv have been the most consistently reachable … large general engines are the ones that come and go" ([ftrcrp guide](https://ftrcrp.org/guides/searxng-docker-tor-setup/)).
- "A Tor-routed instance with two or three engines unresponsive on a given day is working normally" ([gist](https://gist.github.com/RonTuretzky/a2f202dd8989547cc55a1c34ff583434)).
- Engines collapsing to Bing-only on a single IP is a widely reported pattern too ([agenticSeek #410](https://github.com/Fosowl/agenticSeek/issues/410)).

**Net:** Tor is better than our home IP only for engines that rate-limit per IP and do not pre-flag Tor (brave, maybe duckduckgo). It is worse for Google and irrelevant for Startpage and Mojeek.

## 3. Rotation mechanics in Docker

**Images (maintenance checked through the GitHub and Docker Hub APIs on 2026-11-18):**

| Image | State | Notes |
|---|---|---|
| `peterdavehello/tor-socks-proxy` | Repo pushed 2026-11-04, 652 stars, about 4.2M pulls | Tiny (under 10 MB). Tor client only, with no relay or exit function. `SocksPort 0.0.0.0:9150`. "Tor automatically changes IPs every 10 minutes" ([README](https://github.com/PeterDaveHello/tor-socks-proxy)). **Recommended.** |
| `zhaowde/rotating-tor-http-proxy` | Pushed 2026-11-14, 270 stars | HAProxy in front of N Privoxy+Tor pairs, 10 by default and up to 40. New circuit every 30 s, full rebuild every 30 min. `TOR_EXIT_COUNTRY` is available ([README](https://github.com/zhaow-de/rotating-tor-http-proxy)). More moving parts. |
| `dperson/torproxy` | Last Hub push 2021-04-14, repo 2023-12 | **Stale. Avoid**, even though older guides use it. |

**The simplest rotation needs no multi-Tor setup.** Tor's `IsolateSOCKSAuth` is on by default: "Don't share circuits with streams for which different SOCKS authentication was provided" ([Tor manual](https://2019.www.torproject.org/docs/tor-manual.html.en)). Listing the same Tor port N times with different dummy usernames therefore gives N separate circuits, and SearXNG round-robins over them. Circuits refresh after `MaxCircuitDirtiness` (default 10 min, same source). Separate circuits may still share an exit. **That last point is my inference, unverified.**

**Latency vs SearXNG timeouts.** Tor Metrics OnionPerf data for 2026-11-15, downloading 50 KiB from a public server ([torperf.csv](https://metrics.torproject.org/torperf.html)):
- quartiles: about 0.43–1.07 s, 0.71–1.28 s and 1.19–1.76 s across the NL and US vantage points
- round-trip latency median: 240–490 ms ([onionperf-latencies](https://metrics.torproject.org/onionperf-latencies.html))

So the 3 s upstream default is too tight. Our 8 s is fine. A per-engine `timeout: 10` covers the first circuit build. The app's own HTTP timeout to SearXNG is 30 s (`src/pipeline/search.py`), so it absorbs this.

**Resource cost:**
- One Tor client is light. **I found no authoritative figure; unverified.** Expect tens of MB of RAM.
- The HAProxy plus 10×(Tor+Privoxy) image is roughly 10 times that. **Also unverified.**
- Tor carries no API keys or accounts.
- Maintenance: keep the tag updated. If an engine moves to a new anti-bot scheme, Tor won't help (Startpage shows this).

## 4. Risks and alternatives

**Terms of service and ethics**
- Every target disallows automated access to its search path in robots.txt, fetched 2026-11-18: google `Disallow: /search`, bing `/search`, duckduckgo `/html` and `/`, search.brave.com `/search`, search.yahoo.com `/search`.
- Google's ToS forbids "using automated means to access content from any of our services in violation of the machine-readable instructions on our web pages (for example, robots.txt…)" ([Google ToS](https://policies.google.com/terms)).
- **Rotating IPs to evade a rate limit deliberately circumvents a block. That is ethically weaker than scraping from one honest IP, and it pushes our load onto volunteer exit operators** ([Tor exit-list post](https://blog.torproject.org/node/1848) on exits being blocked because of abuse).

**Home network association**
- Only relay IPs appear in the Tor consensus and exit lists, not client IPs ([Tor exit-list post](https://blog.torproject.org/node/1848)). The image runs no relay ([README](https://github.com/PeterDaveHello/tor-socks-proxy)), so our IP does not get listed.
- The ISP can still see that we connect to Tor. This is low risk in most jurisdictions, but **unverified for ours (Asia/Kolkata TZ suggests India)**.

**Reliability:** Tor is "quite error prone" for SearXNG ([PR #3491](https://github.com/searxng/searxng/pull/3491)). Engines will come and go day to day ([gist](https://gist.github.com/RonTuretzky/a2f202dd8989547cc55a1c34ff583434)).

**Alternatives.** Our volume: about 90 runs × about 5 queries ≈ 450 queries/day ≈ **13.5k/month**.

| Option | Current price and free tier (cited) | About 13.5k/month | Fit |
|---|---|---|---|
| **Brave Search API** | $5 per 1,000 requests, "$5 in free credits every month" (about 1,000 queries), 50 qps ([brave.com/search/api](https://brave.com/search/api/)) | about $62/mo, or about $0 if cut to 1k/mo | **SearXNG already ships a `braveapi` engine** (`api_key:`, official API) ([braveapi.py](https://raw.githubusercontent.com/searxng/searxng/master/searx/engines/braveapi.py)). It has been in the code since 2026-02, so it should be in our 2026.7.13 image. Only a settings.yml change is needed, no app code. |
| **Exa** | $10 free credit reset monthly (up to 2,500 Lite searches); Search Lite $4 per 1,000 ([exa.ai/pricing](https://exa.ai/pricing)) | about $44/mo | SearXNG `exaapi` engine was added 2026-07-20, which is **after our image**, so it needs a SearXNG image bump. |
| **Serper** (Google results) | 2,500 free queries at signup; 50k credits at $1.00/1k, 500k at $0.75/1k, down to $0.30/1k; credits valid 6 months ([serper.dev](https://serper.dev/)) | about $13.5/mo | No SearXNG engine found, so it would need app code. |
| **Tavily** | 1,000 credits/month free; basic search = 1 credit; $0.008/credit pay-as-you-go; $30/mo for 4,000 ([pricing](https://www.tavily.com/pricing), [credits](https://docs.tavily.com/documentation/api-credits)) | about $100/mo | Priciest. Needs app code. |
| **Google Programmable Search JSON API** | Closed to new customers; discontinued 2027-01-01; successor Vertex AI Search does not return the public web ([Apify](https://blog.apify.com/google-custom-search-api-alternative/), [Brave summary](https://brave.com/learn/google-api-shutdown/)) | n/a | **Dead end.** |
| Rotating datacenter or residential proxies | Webshare: 10 shared datacenter proxies with 1 GB/mo free; residential from $3.50/GB ([review, third-party](https://proxyway.com/reviews/webshare-proxies)). **Not checked against the vendor's page; unverified.** | a few $/mo | Same ToS issue as Tor. Residential pools often come from ethically murky sources. |
| Fewer queries plus caching | Free | Cuts the 13.5k directly | `src/pipeline/search.py` has no result cache today. A plain SQLite cache with a TTL, plus 5→2–3 queries per item, cuts both the block rate and API cost. |

---

## Simplest concrete setup (if you run the experiment)

Scope: route **only brave and duckduckgo** through Tor. Keep bing, wikipedia, github, hackernews, stackoverflow and semantic scholar direct, since they work. Leave google cse direct or disabled, because Tor makes it worse.

`docker-compose.yml`, new service:

```yaml
  tor:
    image: peterdavehello/tor-socks-proxy:latest
    container_name: pipeline-tor
    restart: unless-stopped
    # no ports: — only searxng reaches it on the compose network (tor:9150)
```

Also add this under the `searxng:` service:

```yaml
    depends_on:
      - tor
```

`searxng/settings.yml`:

```yaml
outgoing:
  # ...existing keys unchanged...
  networks:
    tor:
      # Same Tor daemon; distinct SOCKS usernames => distinct circuits
      # (IsolateSOCKSAuth is on by default). socks5h = DNS via Tor.
      proxies:
        all://:
          - socks5h://c1:x@tor:9150
          - socks5h://c2:x@tor:9150
          - socks5h://c3:x@tor:9150
          - socks5h://c4:x@tor:9150
      using_tor_proxy: true   # fail closed if traffic isn't actually on Tor
      retries: 1              # a retry goes out via the next proxy

search:
  # One CAPTCHA on one exit otherwise suspends the engine for ALL exits for 1h.
  suspended_times:
    SearxEngineCaptcha: 300
    SearxEngineTooManyRequests: 60

engines:
  - name: brave
    network: tor
    timeout: 10.0             # extra_proxy_timeout does NOT apply to these engines
  - name: duckduckgo
    network: tor
    timeout: 10.0
```

Caveats on this setup:
- `suspended_times` is global, so it also shortens suspensions for the direct engines. That is acceptable, but note it.
- I have not verified whether a `using_tor_proxy` key inside an `outgoing.networks` entry is honoured. The code builds networks from those dicts with `using_tor_proxy` as a known attribute, so it should be. If it is not, drop the key.
- Measure before and after with SearXNG `/stats/errors` on the same query set ([SSD Nodes](https://www.ssdnodes.com/learn/searxng-route-engine-requests-through-a-proxy)).

**Expected per-engine success over Tor:** my judgment from the evidence above, not measured.

| Engine | Expectation |
|---|---|
| brave | moderate improvement |
| duckduckgo | moderate, uncertain |
| google cse | worse |
| yahoo | unknown |
| startpage, mojeek | zero (proof of work) |
| bing | keep direct |

---

## Ranked recommendation

1. **Cut volume and cache, and update the SearXNG image.** This is free and simple. Fewer queries per item, a SQLite TTL cache in `search.py`, and pacing. The image bump picks up upstream's `inactive` flags for Startpage and Mojeek, so they stop polluting `unresponsive`.
2. **Enable the `braveapi` engine in settings.yml** as the general-web engine. It is an official API, so there are no CAPTCHAs and no ToS issue. It needs zero app code (one engine block plus a key in env), and $5 of free credit covers about 1k queries a month. After cutting volume, the remainder is about $5 per 1k. Exa (`exaapi`, after an image bump) is the alternative with a larger free allowance.
3. **Serper**, if Google-quality results matter and app code is acceptable. It is the cheapest per query (about $1/1k, $0.30/1k at volume).
4. **Tor sidecar for brave and duckduckgo only**, as a measured experiment. Low cost to try, but unreliable, ethically grey, and it won't touch Startpage, Mojeek or Google.
5. **Commercial rotating proxies.** Same ToS problem as Tor, plus a vendor and a bill. Not recommended.
