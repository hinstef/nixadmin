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
      --env=VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/radeon_icd.x86_64.json \
      --volume=/run/opengl-driver:/run/opengl-driver:ro \
      --volume=/nix/store:/nix/store:ro \
      --volume=/run/nixadmin-helper.sock:/run/nixadmin-helper.sock \
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

  # Privileged helper — runs as root, listens on a Unix socket, executes
  # nixos-rebuild on behalf of the nixadmin group. No sudo involved.
  helperBin = pkgs.writers.writePython3Bin "nixadmin-helper" {
    libraries  = [];  # stdlib only
    flakeIgnore = [ "E501" "E221" ];
  } (builtins.readFile ./helper/nixadmin-helper.py);

  # Derive the nixadmin alias from the tier setting.
  # cloud  → pi's built-in Anthropic OAuth provider (Claude Pro/Max, no API tokens).
  #          Run /login inside pi once to authenticate.
  # remote → OpenAI-compatible API at cfg.remote.baseUrl (LAN server, VPS, etc.)
  # local  → local Ollama container (full privacy, always available).
  nixadminAlias =
    if cfg.tier == "cloud"  then "cd ${cfg.flakeDir} && pi --model anthropic/${cfg.cloud.model}"
    else if cfg.tier == "remote" then "cd ${cfg.flakeDir} && pi --model remote/${cfg.remote.model}"
    else "cd ${cfg.flakeDir} && pi --model ollama/${cfg.local.model}";

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

    environment.systemPackages = [ rebuildBin ];

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

    # Home Manager integration — requires home-manager NixOS module to be imported.
    # Generates pi's models.json and the nixadmin shell alias from the tier options.
    home-manager.users.${cfg.user} = {
      home.file.".pi/agent/models.json".text = modelsJson;
      programs.zsh.shellAliases.nixadmin = nixadminAlias;
    };
  };
}
