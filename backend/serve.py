"""Run the scopegrep scoring service on your own machine.

    python backend/serve.py                         # http://127.0.0.1:8000, no token
    SCOPEGREP_TOKEN=... python backend/serve.py --host 0.0.0.0 --port 8000

Needs one CUDA GPU (see docs/SELF_HOSTING.md for sizes). The model weights
are downloaded from Hugging Face on first start into HF_HOME.

Without SCOPEGREP_TOKEN the service accepts every request, so it refuses to
bind anything but a loopback address unless a token is set.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from service import EXIT_LAYER, MODEL_ID, ScopeGrepService, make_api  # noqa: E402

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default=MODEL_ID, help="Hugging Face model id (default %(default)s)")
    ap.add_argument("--exit-layer", type=int, default=EXIT_LAYER,
                    help="last decoder layer built, 1-indexed (default %(default)s; the paper's)")
    ap.add_argument("--device", default=None, help="torch device (default: cuda if available)")
    args = ap.parse_args()

    token = os.environ.get("SCOPEGREP_TOKEN", "").strip()
    if not token and args.host not in LOOPBACK:
        sys.exit(f"refusing to serve on {args.host} without SCOPEGREP_TOKEN: anyone who can reach "
                 "the port could read the code you send it. Set a token, or bind 127.0.0.1.")

    import uvicorn

    svc = ScopeGrepService(args.model, args.exit_layer, device=args.device)
    uvicorn.run(make_api(svc, token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
