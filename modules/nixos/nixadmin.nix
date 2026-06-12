{ config, lib, pkgs, ... }:

let
  cfg = config.services.nixadmin;

  # Vulkan-accelerated Ollama — works on any GPU with a Vulkan driver.
  # Mesa RADV handles AMD (including Radeon 780M iGPU) via /run/opengl-driver.
  ollamaPackage = pkgs.ollama-vulkan;

  ollamaImage = pkgs.dockerTools.buildLayeredImage {
    name = "nixadmin-ollama";
    tag  = "latest";
    contents = with pkgs; [
      ollamaPackage
      dockerTools.fakeNss   # minimal /etc/passwd, /etc/group
      cacert                # TLS CA bundle for HTTPS model pulls
    ];
    config = {
      Cmd = [ "${ollamaPackage}/bin/ollama" "serve" ];
      Env = [
        "OLLAMA_MODELS=/models"
        "OLLAMA_HOST=127.0.0.1:11434"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "HOME=/root"
      ];
    };
  };

  # Only reload the OCI image when the nix store path changes (i.e. after
  # nixos-rebuild switch). Without caching, podman load runs on every start.
  loadScript = pkgs.writeShellScript "nixadmin-ollama-load" ''
    STAMP="$HOME/.local/state/nixadmin-ollama-image"
    IMAGE="${ollamaImage}"
    if [ "$(cat "$STAMP" 2>/dev/null)" != "$IMAGE" ]; then
      ${pkgs.podman}/bin/podman load -i "$IMAGE" && \
        mkdir -p "$(dirname "$STAMP")" && \
        echo "$IMAGE" > "$STAMP"
    fi
  '';

  startScript = pkgs.writeShellScript "nixadmin-ollama-start" ''
    # Wait for the privileged helper socket before starting so the bind-mount succeeds.
    until [ -S /run/nixadmin-helper.sock ]; do sleep 1; done

    exec ${pkgs.podman}/bin/podman run --rm \
      --name=nixadmin-ollama \
      --volume=nixadmin-models:/models \
      --env=OLLAMA_MODELS=/models \
      --env=OLLAMA_HOST=127.0.0.1:11434 \
      --env=SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
      --env=HOME=/root \
      --env=OLLAMA_VULKAN=1 \
      --env=OLLAMA_IGPU_ENABLE=1 \
      --env=OLLAMA_LOAD_TIMEOUT=20m \
      --env=OLLAMA_KEEP_ALIVE=24h \
      --env=VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/radeon_icd.x86_64.json \
      --volume=/run/opengl-driver:/run/opengl-driver:ro \
      --volume=/nix/store:/nix/store:ro \
      --volume=/run/nixadmin-helper.sock:/run/nixadmin-helper.sock \
      --volume=nixadmin-shader-cache:/root/.cache \
      --tmpfs=/tmp \
      --device=/dev/dri \
      --network=host \
      --security-opt=no-new-privileges \
      nixadmin-ollama:latest
  '';

  # CLI wrapper — lets any user in the nixadmin group run nixos-rebuild
  # via the helper socket with a simple: nixadmin-rebuild <action>
  rebuildBin = pkgs.writers.writePython3Bin "nixadmin-rebuild" {
    libraries  = [];
    flakeIgnore = [ "E501" "E401" ];
  } ''
import socket, json, sys

SOCKET = "/run/nixadmin-helper.sock"
ACTIONS = ["test", "switch", "boot", "revert"]

action = sys.argv[1] if len(sys.argv) > 1 else "test"
if action not in ACTIONS:
    print(f"Usage: nixadmin-rebuild <{'|'.join(ACTIONS)}>", file=sys.stderr)
    sys.exit(1)

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.connect(SOCKET)
except OSError as e:
    print(f"nixadmin-rebuild: cannot connect to {SOCKET}: {e}", file=sys.stderr)
    sys.exit(1)

sock.sendall(json.dumps({"action": action}).encode())
sock.shutdown(socket.SHUT_WR)

buf = b""
while True:
    chunk = sock.recv(4096)
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if not line.strip():
            continue
        msg = json.loads(line)
        if "stream" in msg:
            sys.stdout.write(msg["stream"])
            sys.stdout.flush()
        if "exit" in msg:
            sys.exit(msg["exit"])
'';

  # Toolkit module — pure data + classify/prefetch/augment helpers.
  # No UI, no Nix deps. Imported by nixadmin-rpc. Extend TOOLKIT to add new intents.
  nixadminToolkit = pkgs.writeTextFile {
    name    = "nixadmin-toolkit";
    destination = "/lib/nixadmin_toolkit.py";
    text    = ''
import subprocess, threading, json, urllib.request

TOOLKIT = [
    {
        "name": "apps",
        "description": "installed applications, packages, software, programs, tools, what is installed",
        "commands": ["nixadmin-apps"],
    },
    {
        "name": "network",
        "description": "wifi, wireless, network, internet, connectivity, online, IP address, ping, DNS, ethernet, interface",
        "commands": ["ip link show", "nmcli device status", "ping -c 2 8.8.8.8"],
    },
    {
        "name": "disk",
        "description": "disk space, storage, free space, full, filesystem, drive, partition, mount",
        "commands": ["df -h", "lsblk"],
    },
    {
        "name": "services",
        "description": "running services, systemd, daemons, failed units, background processes, startup",
        "commands": ["systemctl --failed --no-pager", "systemctl --user --failed --no-pager"],
    },
]


def classify(query, model, ollama_url="http://localhost:11434"):
    descriptions = "\n".join("- " + t["name"] + ": " + t["description"] for t in TOOLKIT)
    prompt = (
        "Which categories match this question? "
        "Reply with ONLY a comma-separated list of matching names, or the word 'none'.\n\n"
        "Categories:\n" + descriptions + "\n\nQuestion: " + query + "\n/no_think"
    )
    try:
        data = json.dumps({
            "model": model, "prompt": prompt, "stream": False,
            "options": {"num_predict": 20, "temperature": 0, "think": False},
        }).encode()
        req = urllib.request.Request(
            ollama_url + "/api/generate", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = json.loads(r.read())["response"].strip().lower()
        return [t for t in TOOLKIT if t["name"] in reply]
    except Exception:
        return []


def _run_cmd(cmd):
    try:
        out = subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT, timeout=15, text=True
        )
        return out.strip()
    except subprocess.CalledProcessError as e:
        return (e.output or "").strip() or "(exit " + str(e.returncode) + ")"
    except Exception as e:
        return "(error: " + str(e) + ")"


def prefetch(toolkits):
    if not toolkits:
        return ""
    cmds = [c for t in toolkits for c in t["commands"]]
    results = {}
    lock = threading.Lock()

    def run(cmd):
        out = _run_cmd(cmd)
        with lock:
            results[cmd] = out

    threads = [threading.Thread(target=run, args=(c,)) for c in cmds]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    return "\n\n".join("$ " + c + "\n" + results.get(c, "(timeout)") for c in cmds)


def augment(query, context):
    if not context:
        return query
    return (
        query + "\n\n"
        "[Live system data — use this to answer directly, do not run these commands again:]\n"
        + context
    )
'';
  };

  # RPC wrapper — terminal frontend over nixadmin_toolkit + pi RPC.
  # classify() + prefetch() run behind the spinner before each pi call.
  nixadminRpc = pkgs.writers.writePython3Bin "nixadmin-rpc" {
    libraries  = [];
    flakeIgnore = [ "E501" "E221" "E302" "E303" "E401" "E305" "E402" ];
  } ''
import sys
sys.path.insert(0, "${nixadminToolkit}/lib")
from nixadmin_toolkit import classify, prefetch, augment

import json, subprocess, threading, time, signal, shutil

LOCAL_MODEL = "${cfg.local.model}"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def model_label(args):
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            return args[i + 1].split("/")[-1]
    return ""

def main():
    pi = shutil.which("pi") or "pi"
    cmd = [pi, "--mode", "rpc"] + sys.argv[1:]
    label = model_label(sys.argv[1:])
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError:
        print("nixadmin: pi not found in PATH", file=sys.stderr)
        sys.exit(1)

    spin_idx = [0]
    spin_stop = threading.Event()
    spin_on   = [False]
    spin_thr  = [None]
    streaming  = [False]  # True while assistant text is streaming

    def _spin():
        while not spin_stop.is_set():
            c = SPINNER[spin_idx[0] % len(SPINNER)]
            spin_idx[0] += 1
            sys.stdout.write("\r" + c + " Working...")
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\r                    \r")
        sys.stdout.flush()

    def spin_start():
        if spin_on[0]:
            return
        spin_on[0] = True
        spin_stop.clear()
        spin_thr[0] = threading.Thread(target=_spin, daemon=True)
        spin_thr[0].start()

    def spin_end():
        if not spin_on[0]:
            return
        spin_on[0] = False
        spin_stop.set()
        if spin_thr[0]:
            spin_thr[0].join(timeout=0.5)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    turn_done = threading.Event()

    def on_events():
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "turn_start":
                streaming[0] = False
                spin_start()
            elif t == "message_update":
                ame = ev.get("assistantMessageEvent", {})
                if ame.get("type") == "text_delta":
                    delta = ame.get("delta", "")
                    if delta:
                        spin_end()
                        streaming[0] = True
                        sys.stdout.write(delta)
                        sys.stdout.flush()
            elif t == "agent_end":
                spin_end()
                if streaming[0]:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                streaming[0] = False
                if not ev.get("willRetry"):
                    turn_done.set()
            elif t == "response" and ev.get("id") == "init-probe":
                pi_ready.set()
            elif t == "extension_ui_request":
                spin_end()
                method = ev.get("method", "")
                rid    = ev.get("id", "")
                params = ev.get("params", {})
                if method == "notify":
                    msg = params.get("message", "")
                    if msg:
                        print("\n" + msg)
                elif method == "input":
                    prompt = params.get("title") or params.get("prompt") or "Input"
                    val = input("\n" + prompt + ": ")
                    send({"type": "extension_ui_response", "id": rid, "value": val})
                elif method == "confirm":
                    title = params.get("title") or "Confirm?"
                    msg   = params.get("message", "")
                    label = (title + " " + msg).strip()
                    ans = input("\n" + label + " [y/N] ").strip().lower()
                    send({"type": "extension_ui_response", "id": rid, "confirmed": ans in ("y", "yes")})
                elif method == "select":
                    title   = params.get("title") or "Choose"
                    options = params.get("options", [])
                    print("\n" + title)
                    for i, opt in enumerate(options):
                        lbl = opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)
                        print("  " + str(i + 1) + ") " + lbl)
                    try:
                        idx = int(input("Choice: ").strip()) - 1
                        val = options[max(0, min(idx, len(options) - 1))]
                    except (ValueError, IndexError):
                        val = options[0] if options else ""
                    send({"type": "extension_ui_response", "id": rid, "value": val})

    pi_ready = threading.Event()
    thr = threading.Thread(target=on_events, daemon=True)
    thr.start()

    # Probe pi readiness — send get_state after 1s and wait for its response.
    time.sleep(1)
    send({"type": "get_state", "id": "init-probe"})
    pi_ready.wait(timeout=5)

    def on_sigint(sig, frame):
        spin_end()
        proc.terminate()
        print()
        sys.exit(0)
    signal.signal(signal.SIGINT, on_sigint)

    prompt_str = ("\nnixadmin [" + label + "]> ") if label else "\nnixadmin> "

    try:
        while proc.poll() is None:
            try:
                line = input(prompt_str).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            # Classify intent → pre-fetch live data → augment query — all behind spinner.
            spin_start()
            toolkits = classify(line, LOCAL_MODEL)
            context  = prefetch(toolkits)
            spin_end()

            turn_done.clear()
            send({"type": "prompt", "message": augment(line, context)})
            turn_done.wait()
    finally:
        spin_end()
        proc.terminate()

main()
'';

  # Privileged helper — runs as root, listens on a Unix socket, executes
  # nixos-rebuild on behalf of the nixadmin group. No sudo involved.
  helperBin = pkgs.writers.writePython3Bin "nixadmin-helper" {
    libraries  = [];  # stdlib only
    flakeIgnore = [ "E501" "E221" ];
  } (builtins.readFile ./helper/nixadmin-helper.py);

  # Resolve the pi --model flag for the configured tier.
  piModel =
    if cfg.tier == "cloud"  then "anthropic/${cfg.cloud.model}"
    else if cfg.tier == "remote" then "remote/${cfg.remote.model}"
    else "ollama/${cfg.local.model}";

  # General nixadmin wrapper — handles auth hint, machine profile generation/injection,
  # and session counting. Used for all tiers.
  nixadminWrapper = pkgs.writeShellScript "nixadmin" ''
    PROFILE_DIR="$HOME/.local/share/nixadmin"
    PROFILE_FILE="$PROFILE_DIR/machine-profile.md"
    SESSION_FILE="$PROFILE_DIR/session-count"
    REFRESH_INTERVAL=${toString cfg.profileRefreshSessions}

    # Cloud tier: hint if not authenticated.
    ${lib.optionalString (cfg.tier == "cloud") ''
      if ! ${pkgs.jq}/bin/jq -e '.anthropic.type == "oauth"' \
          "$HOME/.pi/agent/auth.json" > /dev/null 2>&1; then
        echo "Not logged in to Claude. Type /login inside nixadmin to authenticate."
      fi
    ''}

    generate_profile() {
      mkdir -p "$PROFILE_DIR"
      ${lib.optionalString (cfg.tier == "local") ''
        echo "Generating machine profile using local model — this may take a few minutes..."
      ''}
      ${lib.optionalString (cfg.tier != "local") ''
        echo "Generating machine profile..."
      ''}

      CONTEXT=$(
        echo "=== NixOS config ==="
        ${pkgs.coreutils}/bin/cat \
          ${cfg.flakeDir}/flake.nix \
          ${cfg.flakeDir}/hosts/*/default.nix \
          ${cfg.flakeDir}/modules/nixos/common.nix 2>/dev/null | head -300
        echo ""
        echo "=== Installed packages ==="
        nixadmin-apps 2>/dev/null
        echo ""
        echo "=== Network interfaces ==="
        ${pkgs.iproute2}/bin/ip link show 2>/dev/null
        echo ""
        echo "=== CPU ==="
        ${pkgs.util-linux}/bin/lscpu 2>/dev/null | grep -E "^Model name|^CPU\(s\)|^Architecture"
        echo ""
        echo "=== Disk ==="
        ${pkgs.coreutils}/bin/df -h 2>/dev/null | grep -v tmpfs
      )

      pi --print --no-session --no-tools \
        --model ${piModel} \
        --system-prompt "You generate machine profiles for a NixOS sysadmin AI. Be concise and factual. Plain text only, no markdown headers or bullets." \
        "Generate a machine profile (max 300 words) from the system info below. Cover: exact network interface names, CPU/GPU, key installed apps, filesystem layout, and any non-obvious machine-specific details an AI sysadmin should know.

$CONTEXT" > "$PROFILE_FILE" 2>/dev/null

      if [ -s "$PROFILE_FILE" ]; then
        echo "Machine profile ready."
      else
        echo "Profile generation failed, continuing without profile."
        rm -f "$PROFILE_FILE"
      fi
    }

    # Generate profile on first launch.
    if [ ! -f "$PROFILE_FILE" ]; then
      generate_profile
    fi

    # Increment session counter; offer refresh every N sessions.
    COUNT=$(${pkgs.coreutils}/bin/cat "$SESSION_FILE" 2>/dev/null || echo 0)
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$SESSION_FILE"
    if [ "$COUNT" -gt 0 ] && [ $(( COUNT % REFRESH_INTERVAL )) -eq 0 ]; then
      read -p "Machine profile is $COUNT sessions old. Refresh? [y/N] " -n 1 -r
      echo
      if [[ $REPLY =~ ^[Yy]$ ]]; then
        generate_profile
      fi
    fi

    # Hide thinking blocks in pi settings (safe to set on every launch).
    SETTINGS="$HOME/.pi/agent/settings.json"
    if ! ${pkgs.jq}/bin/jq -e '.hideThinkingBlock == true' "$SETTINGS" > /dev/null 2>&1; then
      TMP=$(${pkgs.jq}/bin/jq '. + {"hideThinkingBlock": true, "quietStartup": true}' "$SETTINGS" 2>/dev/null \
        || echo '{"hideThinkingBlock": true, "quietStartup": true}')
      echo "$TMP" > "$SETTINGS"
    fi

    # Inject profile into system prompt if available.
    APPEND_ARGS=()
    if [ -s "$PROFILE_FILE" ]; then
      APPEND_ARGS=(--append-system-prompt "$(${pkgs.coreutils}/bin/cat "$PROFILE_FILE")")
    fi

    # Pick model: `nixadmin --local` overrides to on-device Ollama.
    MODEL=${piModel}
    if [ "''${1:-}" = "--local" ]; then
      MODEL="ollama/${cfg.local.model}"
      shift
    fi

    # When using local model: wait for it to be loaded into GPU before starting.
    if [[ "$MODEL" == ollama/* ]]; then
      LOCAL_READY=0
      if ${pkgs.curl}/bin/curl -sf http://127.0.0.1:11434/api/ps 2>/dev/null \
          | ${pkgs.gnugrep}/bin/grep -q "${cfg.local.model}"; then
        LOCAL_READY=1
      fi
      if [ "$LOCAL_READY" -eq 0 ]; then
        if ! ${pkgs.curl}/bin/curl -sf http://127.0.0.1:11434/ > /dev/null 2>&1; then
          echo "Local model is starting up — waiting for Ollama..."
        else
          echo "Local model is loading into GPU memory — this may take a minute..."
        fi
        until ${pkgs.curl}/bin/curl -sf http://127.0.0.1:11434/api/ps 2>/dev/null \
            | ${pkgs.gnugrep}/bin/grep -q "${cfg.local.model}"; do
          sleep 2
        done
        echo "Model ready."
      fi
    fi

    cd ${cfg.flakeDir}
    exec ${nixadminRpc}/bin/nixadmin-rpc --model "$MODEL" --thinking off "''${APPEND_ARGS[@]}"
  '';

  nixadminAlias = "${nixadminWrapper}";

  # Generate models.json for pi. The anthropic (cloud) provider is built-in
  # so it is never listed here — credentials come from /login OAuth flow.
  # Remote provider is only included when a baseUrl is configured.
  modelsJson = builtins.toJSON {
    providers =
      { ollama = {
          baseUrl = "http://localhost:11434/v1";
          api     = "openai-completions";
          apiKey  = "ollama";
          models  = [{ id = cfg.local.model; }];
        };
      }
      // lib.optionalAttrs (cfg.remote.baseUrl != "") {
        remote = {
          baseUrl = cfg.remote.baseUrl;
          api     = "openai-completions";
          apiKey  = "none";
          models  = [{ id = cfg.remote.model; }];
        };
      };
  };

in {

  options.services.nixadmin = {
    enable = lib.mkEnableOption "nixadmin AI sysadmin";

    user = lib.mkOption {
      type        = lib.types.str;
      example     = "alice";
      description = ''
        User who runs the Ollama container (rootless Podman). Added to the
        `nixadmin` group for helper socket access, and to `render`/`video`
        for Vulkan/DRI GPU access.
      '';
    };

    flakeDir = lib.mkOption {
      type        = lib.types.str;
      example     = "/home/alice/nixos-config";
      description = ''
        Absolute path to the NixOS configuration flake on the host.
        Used as the --flake prefix when the helper runs nixos-rebuild.
      '';
    };

    hostname = lib.mkOption {
      type        = lib.types.str;
      example     = "myhost";
      description = ''
        Flake output name for the NixOS configuration, i.e. the part after
        the # in --flake <flakeDir>#<hostname>.
      '';
    };

    tier = lib.mkOption {
      type    = lib.types.enum [ "cloud" "remote" "local" ];
      default = "local";
      description = ''
        Which model tier the `nixadmin` alias targets.
          cloud  — Claude Pro/Max via pi's built-in Anthropic OAuth (run /login once).
          remote — Self-hosted OpenAI-compatible API (LAN server, VPS, etc.).
          local  — Local Ollama container (full privacy, always available).
        All tiers are always configured in models.json; this only sets the default.
      '';
    };

    cloud.model = lib.mkOption {
      type    = lib.types.str;
      default = "claude-sonnet-4-5";
      description = "Claude model to use for the cloud tier.";
    };

    remote.baseUrl = lib.mkOption {
      type    = lib.types.str;
      default = "";
      example = "http://homeserver:11434/v1";
      description = "Base URL of the remote OpenAI-compatible API. Leave empty to omit the remote provider.";
    };

    remote.model = lib.mkOption {
      type    = lib.types.str;
      default = "llama3.3:70b";
      description = "Model to use on the remote server.";
    };

    local.model = lib.mkOption {
      type    = lib.types.str;
      default = "qwen3-tool:latest";
      description = "Local Ollama model to use.";
    };

    profileRefreshSessions = lib.mkOption {
      type    = lib.types.int;
      default = 10;
      description = "Prompt to refresh the machine profile every N sessions.";
    };
  };

  config = lib.mkIf cfg.enable {

    # Dedicated group — the helper socket is owned root:nixadmin mode 0660.
    users.groups.nixadmin = {};

    users.users.${cfg.user} = {
      extraGroups = [ "nixadmin" "render" "video" ];
      # Required for rootless Podman user namespace mapping.
      subUidRanges = [{ startUid = 100000; count = 65536; }];
      subGidRanges = [{ startGid = 100000; count = 65536; }];
    };

    # Enable linger so the user service starts at boot without an active session.
    systemd.tmpfiles.rules = [
      "f /var/lib/systemd/linger/${cfg.user} 0644 root root -"
    ];

    environment.systemPackages = [ rebuildBin nixadminRpc ];

    virtualisation.podman.enable = true;

    # Privileged helper systemd service.
    # Runs as root; the socket is group-accessible to the nixadmin group only.
    # Replaces NOPASSWD sudoers — nothing in this module touches security.sudo.
    systemd.services.nixadmin-helper = {
      description = "nixadmin privileged rebuild helper";
      after       = [ "network.target" ];
      wantedBy    = [ "multi-user.target" ];

      environment = {
        NIXADMIN_FLAKE_DIR = cfg.flakeDir;
        NIXADMIN_HOSTNAME  = cfg.hostname;
      };

      serviceConfig = {
        Type       = "simple";
        User       = "root";
        ExecStart  = "${helperBin}/bin/nixadmin-helper";
        Restart    = "on-failure";
        RestartSec = "3s";
        PrivateTmp = true;
        # Only kill the helper process itself, not its children (e.g. nixos-rebuild).
        # Without this, systemd kills the nixos-rebuild subprocess when stopping the
        # service during a switch, preventing the switch from completing.
        KillMode   = "process";
        # ProtectHome must be off — nixos-rebuild reads the flake from the user's home dir.
        # Do NOT set ProtectSystem — nixos-rebuild writes to /nix/store.
      };
    };

    # Rootless Podman container — runs as cfg.user, no sudo needed.
    # Starts after the graphical session to stay off the boot critical path.
    systemd.user.services.nixadmin-ollama = {
      description = "nixadmin Ollama inference (rootless Podman, Vulkan)";
      after       = [ "graphical-session.target" ];
      wantedBy    = [ "graphical-session.target" ];

      serviceConfig = {
        Type = "simple";
        Environment = "PATH=/run/wrappers/bin:/run/current-system/sw/bin";
        ExecStartPre = [
          # Load image only when the store path has changed (post nixos-rebuild switch).
          "${loadScript}"
          # Remove any stale container from a previous unclean exit.
          "-${pkgs.podman}/bin/podman rm -f nixadmin-ollama"
        ];
        ExecStart  = "${startScript}";
        ExecStop   = "${pkgs.podman}/bin/podman stop nixadmin-ollama";
        Restart    = "on-failure";
        RestartSec = "5s";
      };
    };

    # Oneshot service — waits for Ollama then pre-loads the local model into GPU memory.
    # Runs as its own systemd unit so it isn't killed when the start script exits.
    systemd.user.services.nixadmin-ollama-preload = {
      description = "Pre-load nixadmin local model into GPU memory";
      after    = [ "nixadmin-ollama.service" ];
      requires = [ "nixadmin-ollama.service" ];
      wantedBy = [ "nixadmin-ollama.service" ];

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        Environment = "PATH=/run/wrappers/bin:/run/current-system/sw/bin";
        ExecStart = pkgs.writeShellScript "nixadmin-ollama-preload" ''
          until ${pkgs.curl}/bin/curl -sf http://127.0.0.1:11434/ > /dev/null 2>&1; do
            sleep 2
          done
          ${pkgs.curl}/bin/curl -sf -X POST http://127.0.0.1:11434/api/generate \
            --max-time 1200 \
            -d '{"model":"${cfg.local.model}","prompt":"","keep_alive":-1}' \
            > /dev/null 2>&1
        '';
      };
    };

    # Home Manager integration — requires home-manager NixOS module to be imported.
    # Generates pi's models.json and the nixadmin shell alias from the tier options.
    home-manager.users.${cfg.user} = {
      home.file.".pi/agent/models.json".text = modelsJson;
      programs.zsh.shellAliases.nixadmin = nixadminAlias;
    };
  };
}
