"""Run the scopegrep scoring service on Modal (https://modal.com).

    TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    modal secret create scopegrep-auth SCOPEGREP_TOKEN="$TOKEN"
    modal deploy backend/modal_app.py

The deploy prints the endpoint URL; point the client's SCOPEGREP_URL at it and
SCOPEGREP_TOKEN at $TOKEN. Full guide: docs/SELF_HOSTING.md.

Same service as `serve.py` (both wrap service.ScopeGrepService), on one GPU
container that scales to zero when idle. max_containers=1 is deliberate: the
warm scope caches live in that container's GPU memory, so one container means
the next query always finds them, and it bounds the bill.

Settings, read at deploy time:
  SCOPEGREP_GPU                 Modal GPU spec            (default A100-80GB, the paper's)
  SCOPEGREP_SCALEDOWN           idle seconds before teardown (default 300)
  SCOPEGREP_MIN_CONTAINERS      containers held warm; bills continuously when > 0 (default 0)
  SCOPEGREP_MODEL_ID            scoring model             (default Qwen/Qwen3.5-9B)
  SCOPEGREP_MAX_CACHED_TOKENS   scope tokens kept warm    (default 600000)
"""
import os
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
GPU = os.environ.get("SCOPEGREP_GPU", "A100-80GB")
SCALEDOWN = int(os.environ.get("SCOPEGREP_SCALEDOWN", "300"))
MIN_CONTAINERS = int(os.environ.get("SCOPEGREP_MIN_CONTAINERS", "0"))
# Baked into the image so the container sees the deploy-time choice;
# os.environ inside a Modal container is not the deployer's environment.
SETTINGS = {k: os.environ[k] for k in ("SCOPEGREP_MODEL_ID", "SCOPEGREP_EXIT_LAYER",
                                       "SCOPEGREP_MAX_CACHED_TOKENS", "SCOPEGREP_MAX_CHUNKS")
            if k in os.environ}

# Same base and pins as the paper's evaluation runs. No flash-attn: the
# prefill uses sdpa and the attention readout needs eager (materialized
# weights), so a flash-attn wheel would never be on the code path.
image = (
    modal.Image.from_registry("nvidia/cuda:13.0.1-devel-ubuntu22.04", add_python="3.12")
    .pip_install("torch==2.12.0", "transformers==5.8.0", "accelerate==1.13.0", "safetensors",
                 "fastapi[standard]")
    .env({"HF_HOME": "/cache/huggingface", "HF_HUB_ENABLE_HF_TRANSFER": "0",
          "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True", **SETTINGS})
)
for name in ("two_pass_core", "layer_exit_core", "scope_cache_core", "service"):
    image = image.add_local_file(str(HERE / f"{name}.py"), f"/root/{name}.py")

hf_cache = modal.Volume.from_name("scopegrep-hf-cache", create_if_missing=True)
auth = modal.Secret.from_name("scopegrep-auth", required_keys=["SCOPEGREP_TOKEN"])
app = modal.App("scopegrep", image=image)


@app.cls(gpu=GPU, volumes={"/cache": hf_cache}, secrets=[auth], timeout=1800,
         scaledown_window=SCALEDOWN, min_containers=MIN_CONTAINERS, max_containers=1)
class ScopeGrep:
    @modal.enter()
    def load(self):
        from service import ScopeGrepService
        self.svc = ScopeGrepService()
        hf_cache.commit()       # keep the downloaded weights for the next cold start

    @modal.asgi_app()
    def web(self):
        from service import make_api
        token = os.environ["SCOPEGREP_TOKEN"]
        if not token.strip():
            raise RuntimeError("the scopegrep-auth secret has an empty SCOPEGREP_TOKEN")
        # a public endpoint always requires the token, unlike a loopback serve.py
        return make_api(self.svc, token, extra_health={
            "gpu": GPU, "scaledown_window_seconds": SCALEDOWN, "min_containers": MIN_CONTAINERS})
