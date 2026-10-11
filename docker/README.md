# SingularOS Docker Environment

Two services for developing SingularOS on any machine, including Apple Silicon:

- **omarchy** — the Omarchy (Arch + Hyprland) desktop running in a QEMU VM,
  displayed in the browser via noVNC.
- **llm** — a local LLM server ([llama.cpp](https://github.com/ggml-org/llama.cpp)
  `llama-server`) exposing an OpenAI-compatible API with grammar-constrained
  decoding (`response_format: json_schema` / raw GBNF `grammar` parameter).

The Omarchy image is adapted from
[domarchy](https://github.com/overthinkinglabs/domarchy) (MIT, Copyright (c)
2025 overthinkinglabs).

## Prerequisites

- Docker Engine (or Docker Desktop) with Compose v2.
- macOS: allocate at least 8 GB of RAM to Docker Desktop
  (Settings → Resources). More is needed for larger models.
- Linux with NVIDIA GPUs: install the
  [nvidia-container-toolkit](https://github.com/NVIDIA/nvidia-container-toolkit)
  and switch the `llm` service image to
  `ghcr.io/ggml-org/llama.cpp:server-cuda` (see below).

## Quickstart

```sh
cd docker
cp .env.example .env
docker compose build
docker compose up -d
```

### Omarchy desktop

Open `http://localhost:8900` in a browser. The first run downloads the Omarchy
ISO and boots the installer; complete it manually. Subsequent starts boot the
installed system directly.

- The VM disk persists in the `omarchy-data` named volume.
- The `shared/` folder is exposed to the guest via Samba
  (`smb://10.0.2.4/qemu` from inside the VM).
- Keyboard shortcuts: if your host intercepts Super-key shortcuts, see the
  passthrough note in the domarchy README.

### LLM server

1. Download any GGUF model into `docker/models/` (not committed to git).
2. Set `LLM_MODEL=<file>.gguf` in `.env`.
3. `docker compose up -d llm`

Verify:

```sh
curl http://localhost:8080/health
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Constrained output (forced JSON schema):

```sh
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages":[{"role":"user","content":"Report vehicle status"}],
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "vehicle_status",
        "schema": {
          "type": "object",
          "properties": {
            "mode": {"type": "string", "enum": ["NORMAL","INVESTIGATE","RETURN","APPROACH","DOCKED","HOLD","ABORT"]},
            "confidence": {"type": "number"}
          },
          "required": ["mode","confidence"]
        }
      }
    }
  }'
```

Raw GBNF grammars are also supported via the `grammar` parameter. See the
[llama.cpp grammars docs](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md).

## Platform notes

- **Apple Silicon (arm64):** the `llm` image runs natively as arm64. The
  Omarchy guest is emulated x86_64 (QEMU TCG) — functional but slower than
  native. Expect the desktop to be usable; heavy GPU workloads inside the
  guest are not.
- **Linux with KVM (amd64):** the Omarchy guest runs near-native.
- **NVIDIA GPUs:** change the `llm` service to use
  `ghcr.io/ggml-org/llama.cpp:server-cuda` (or `server-cuda13`) and add:

  ```yaml
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
  ```

- **AMD GPUs / Intel / Vulkan:** equivalent `server-rocm`, `server-intel`,
  and `server-vulkan` tags exist upstream.

## Model management

Models are GGUF files mounted read-only from `docker/models/`. Nothing is
downloaded at build time and nothing model-related is stored in git. Any
GGUF-format model works; choose one that fits your host's RAM.

## Relationship to the mission spec

Per `MISSION.md` §2, SingularOS is not Omarchy-dependent. This environment is
the operator/development track: the simulator and agent can run against the
`llm` service (or any OpenAI-compatible endpoint) without requiring the
desktop. The desktop exists for operating the system the way the target
environment expects.

## Files

```
docker/
├── compose.yaml          # service definitions
├── .env.example          # configuration template
├── omarchy/
│   ├── Dockerfile        # QEMU + noVNC image (adapted from domarchy, MIT)
│   └── run-vm.sh         # VM boot script
└── llm/
    └── Dockerfile        # llama.cpp server wrapper
```
