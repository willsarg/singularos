# Local LLM runtime with constrained-output support.
#
# Uses the official multi-arch llama.cpp image (amd64 + arm64). llama-server
# exposes an OpenAI-compatible API with grammar-constrained decoding:
#   - response_format json_schema (schema compiled to GBNF grammar)
#   - raw grammar= parameter (GBNF)
# See https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
#
# Models are GGUF files provided at runtime via the models volume; none are
# stored in this repository.

FROM ghcr.io/ggml-org/llama.cpp:server

# GGUF models are mounted at /models. Nothing is pulled at build time.
VOLUME ["/models"]

EXPOSE 8080

# The container starts with no model loaded. Start one by overriding the
# command (see docker/README.md) or via the compose service config:
#   llama-server -m /models/<model>.gguf --host 0.0.0.0 --port 8080 \
#       --jinja --parallel 4 --ctx-size 8192
CMD ["llama-server", "--help"]
