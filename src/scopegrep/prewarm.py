"""Force the scoring service to load its model before anybody is waiting on it.

The service scales to zero. `/health` is served from inside the GPU class, so
calling it starts a container and runs the 9B model load -- measured at
71-117s in the benchmark transcripts. That cost is paid by whoever queries
first. On your own machine that is an annoyance; in front of a pilot partner
it is the first thing they see, and it is not what the tool costs in steady
state.

This is the pip-installable twin of `tools/prewarm.sh` -- same behaviour, no
bash/curl dependency, for anyone who installed the package rather than the
plugin directory directly. Keep the two in sync; they read the same two
environment variables and hit the same endpoint.

    scopegrep-prewarm              # once, ~2 min before the session
    scopegrep-prewarm --watch 120  # hold it warm for a demo window
"""
import argparse
import json
import os
import sys
import time

import httpx

TOKEN_FILE = os.path.expanduser("~/.config/scopegrep/token")


def _resolve_creds():
    url = os.environ.get("SCOPEGREP_URL", "")
    token = os.environ.get("SCOPEGREP_TOKEN", "")
    if not token and os.path.isfile(TOKEN_FILE):
        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    if not url or not token:
        sys.exit("prewarm: set SCOPEGREP_URL and SCOPEGREP_TOKEN (or write the "
                 "token to ~/.config/scopegrep/token)")
    return url.rstrip("/"), token


def warm_once(url, token):
    """One `/health` call. Returns True on success, False on failure -- never
    raises, so a `--watch` loop survives a transient network blip."""
    t0 = time.time()
    try:
        # timeout must exceed a cold model load or this reports a failure for
        # a service that is working exactly as designed.
        r = httpx.get(f"{url}/health", timeout=900.0, follow_redirects=True,
                      headers={"X-Scopegrep-Token": token})
    except Exception as e:                                      # noqa: BLE001
        print(f"{_now()}  prewarm FAILED after {time.time()-t0:.0f}s: {e}",
              file=sys.stderr)
        return False
    elapsed = time.time() - t0
    if r.status_code != 200:
        print(f"{_now()}  prewarm FAILED after {elapsed:.0f}s: "
              f"HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    # Report whether this call paid the load or found it already up -- the
    # two look identical from the exit code and only one of them is the point.
    try:
        h = r.json()
        status = (f"{h['container_uptime_seconds']:.0f}s up, "
                  f"load {h['model_load_seconds']}s, "
                  f"{len(h['cached_scopes'])} warm scope(s), "
                  f"min_containers={h.get('min_containers', '?')}")
    except (json.JSONDecodeError, KeyError):
        status = "ok"
    print(f"{_now()}  warm in {elapsed:.0f}s  [{status}]")
    return True


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--watch", type=float, metavar="SECONDS",
                    help="keep re-touching /health every SECONDS, inside the "
                         "scaledown window, instead of a held container")
    args = ap.parse_args()
    url, token = _resolve_creds()

    if args.watch:
        print(f"prewarm: touching {url}/health every {args.watch:g}s "
              f"(ctrl-c to stop)")
        while True:
            warm_once(url, token)
            time.sleep(args.watch)
    else:
        sys.exit(0 if warm_once(url, token) else 1)


if __name__ == "__main__":
    main()
