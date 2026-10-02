# backend

The scopegrep scoring service. Setup, GPU sizing and the Modal deploy are in
[docs/SELF_HOSTING.md](../docs/SELF_HOSTING.md).

| file | what it is |
|---|---|
| `service.py` | the service: model, scope caches, Scope + Fine Attention, HTTP API |
| `serve.py` | run it on your own GPU (`python backend/serve.py`) |
| `modal_app.py` | run it on Modal (`modal deploy backend/modal_app.py`) |
| `smoke.py` | end-to-end check of a running service |
| `two_pass_core.py`, `layer_exit_core.py`, `scope_cache_core.py` | the research scoring code the paper's numbers were measured with, unmodified. `two_pass_core.py` also holds the earlier excerpt-based two-pass scorer; the service uses only its prompt and cache helpers. |
| `requirements.txt` | the pins the paper's runs used |
