self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.nixadmin;
  pkg = self.packages.${pkgs.system}.nixadmin;

  # The daemon runs in a Python env that includes nixadmin plus any extra module
  # packages, so importlib entry-point discovery finds the modules they register.
  daemonPython = pkgs.python3.withPackages (_: [ pkg ] ++ cfg.extraModules);

  # The tray and web view are lightweight clients — just nixadmin (for protocol)
  # + its deps (dbus-fast, structlog). No extra modules, no daemon internals.
  clientPython = pkgs.python3.withPackages (_: [ pkg ]);

  # Privileged rebuild helper — runs as root, owns a group-accessible socket.
  helper = pkgs.writers.writePython3Bin "nixadmin-helper"
    { flakeIgnore = [ "E221" "E501" ]; }
    (builtins.readFile ./nixadmin-helper.py);

  # `nixadmin-apps` — the apps module's fetcher command. Lists declarative Nix
  # packages and installed Flatpak apps in plain text.
  nixadminApps = pkgs.writeShellScriptBin "nixadmin-apps" ''
    echo "=== Nix packages ==="
    ${pkgs.gnugrep}/bin/grep -E '^\s+[a-z][a-zA-Z0-9_.-]+\s*$' \
      ${cfg.flakeDir}/modules/home-manager/default.nix 2>/dev/null \
      | ${pkgs.gnused}/bin/sed 's/^\s*//' || true
    echo ""
    echo "=== Flatpak apps ==="
    ${pkgs.flatpak}/bin/flatpak list --app --columns=name,application 2>/dev/null || true
  '';

  # ---- Vulkan-accelerated Ollama in a rootless Podman container ---------- #
  ollamaImage = pkgs.dockerTools.buildLayeredImage {
    name = "nixadmin-ollama";
    tag = "latest";
    contents = with pkgs; [ ollama-vulkan dockerTools.fakeNss cacert ];
    config = {
      Cmd = [ "${pkgs.ollama-vulkan}/bin/ollama" "serve" ];
      Env = [
        "OLLAMA_MODELS=/models"
        "OLLAMA_HOST=127.0.0.1:11434"
        "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
        "HOME=/root"
      ];
    };
  };

  # Load the OCI image only when its store path changed (post rebuild switch).
  ollamaLoad = pkgs.writeShellScript "nixadmin-ollama-load" ''
    STAMP="$HOME/.local/state/nixadmin-ollama-image"
    IMAGE="${ollamaImage}"
    if [ "$(cat "$STAMP" 2>/dev/null)" != "$IMAGE" ]; then
      ${pkgs.podman}/bin/podman load -i "$IMAGE" && \
        mkdir -p "$(dirname "$STAMP")" && echo "$IMAGE" > "$STAMP"
    fi
  '';

  ollamaStart = pkgs.writeShellScript "nixadmin-ollama-start" ''
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
      --env=OLLAMA_KEEP_ALIVE=${cfg.local.keepAlive} \
      --env=VK_ICD_FILENAMES=${cfg.ollama.vulkanIcd} \
      --volume=/run/opengl-driver:/run/opengl-driver:ro \
      --volume=/nix/store:/nix/store:ro \
      --volume=nixadmin-shader-cache:/root/.cache \
      --tmpfs=/tmp \
      --device=/dev/dri \
      --network=host \
      --security-opt=no-new-privileges \
      nixadmin-ollama:latest
  '';

  # Env passed to the daemon (a user service) — mirrors Config.from_env().
  daemonEnv = {
    NIXADMIN_FLAKE_DIR = cfg.flakeDir;
    NIXADMIN_HOSTNAME = cfg.hostname;
    NIXADMIN_LOCAL_MODEL = cfg.local.model;
    NIXADMIN_LOCAL_URL = cfg.local.url;
    NIXADMIN_REMOTE_MODEL = cfg.remote.model;
    NIXADMIN_REMOTE_BASE = cfg.remote.base;
    NIXADMIN_CHAIN = cfg.defaultChain;
    NIXADMIN_HISTORY = cfg.history;
    NIXADMIN_EVENTS = cfg.events;
    NIXADMIN_AUTOFIX = if cfg.autofix.enable then "1" else "0";
    NIXADMIN_AUTOFIX_SYSTEM = if cfg.autofix.system then "1" else "0";
    NIXADMIN_AUTOFIX_MAX_ATTEMPTS = toString cfg.autofix.maxAttempts;
    NIXADMIN_LOG_FORMAT = cfg.logFormat;
    # Fetcher commands (nmcli, ping, lsblk, nixadmin-apps…) resolve via the system
    # profile; git + nix are needed by the action tier (worktree-validated edits).
    # mkForce overrides the default user-service PATH rather than conflicting.
    PATH = lib.mkForce "${pkgs.git}/bin:${pkgs.nix}/bin:/run/wrappers/bin:/run/current-system/sw/bin";
  } // lib.optionalAttrs (cfg.stateDir != "") {
    # Where the persistent event store lives. Left unset by default so the daemon
    # derives it from the user's XDG_STATE_HOME (~/.local/state/nixadmin) at runtime.
    NIXADMIN_STATE_DIR = cfg.stateDir;
  };

  ollamaEnabled = cfg.ollama.enable && cfg.local.model != "";
in
{
  options.services.nixadmin = {
    enable = lib.mkEnableOption "nixadmin ambient system intelligence daemon";

    user = lib.mkOption {
      type = lib.types.str;
      description = "User that runs the daemon (a systemd user service) and Ollama.";
    };

    flakeDir = lib.mkOption {
      type = lib.types.str;
      description = "Absolute path to the NixOS flake (the helper rebuilds from here).";
    };

    hostname = lib.mkOption {
      type = lib.types.str;
      description = "Flake output name (the part after # in --flake <dir>#<host>).";
    };

    defaultChain = lib.mkOption {
      type = lib.types.enum [ "local" "remote" ];
      default = "remote";
      description = "Default chain when a query does not specify one.";
    };

    local.model = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Local Ollama model. Empty means no local chain (remote-only machine).";
    };

    local.url = lib.mkOption {
      type = lib.types.str;
      default = "http://localhost:11434";
      description = "Base URL of the local Ollama server.";
    };

    local.keepAlive = lib.mkOption {
      type = lib.types.str;
      default = "10m";
      example = "30m";
      description = ''
        How long Ollama keeps the model resident after a request
        (OLLAMA_KEEP_ALIVE). The model loads on demand and unloads after this idle
        window, so an idle machine reclaims the RAM/VRAM. The daemon shows a
        "warming up" status and classifies through the cold load (~6s), so a cold
        first query is slow-but-correct rather than a false "all clear". Use "-1"
        to pin it resident (not recommended on a laptop).
      '';
    };

    remote.model = lib.mkOption {
      type = lib.types.str;
      default = "claude-sonnet-4-5";
      description = "Remote model id (LiteLLM format).";
    };

    remote.base = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "http://localhost:4000";
      description = "Remote API base URL (Hermes proxy or direct). Empty = LiteLLM default.";
    };

    history = lib.mkOption {
      type = lib.types.enum [ "null" ];
      default = "null";
      description = "History backend (only 'null' in v1).";
    };

    events = lib.mkOption {
      type = lib.types.enum [ "sqlite" "null" ];
      default = "sqlite";
      description = ''
        Persistent system-event timeline backend (the observability substrate
        behind the web hub). "sqlite" writes <stateDir>/events.db; "null" disables
        persistence.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "/home/alice/.local/state/nixadmin";
      description = ''
        Directory for the daemon's persistent state (the event store). Empty (the
        default) lets the daemon derive it from the user's XDG_STATE_HOME, i.e.
        ~/.local/state/nixadmin.
      '';
    };

    autofix.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Automatically restart failed systemd units (with a restart-loop guard and
        honest verification), recording every action to the event timeline. A
        failed unit is already broken, so a verified restart is the safe,
        reversible "act, don't ask" case (see docs/ux.md).
      '';
    };

    autofix.system = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Also auto-restart failed system-scope units (via the privileged helper),
        not just user-session units. Disable to only auto-fix user units and merely
        surface system failures.
      '';
    };

    autofix.maxAttempts = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1;
      description = ''
        How many times autofix will restart a given unit within an hour before it
        stops and instead reports that the unit needs a real fix (loop guard).
      '';
    };

    logFormat = lib.mkOption {
      type = lib.types.enum [ "json" "console" ];
      default = "json";
      description = "Daemon log renderer.";
    };

    extraModules = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      example = lib.literalExpression "[ inputs.nixadmin.packages.\${system}.nixadmin-extras ]";
      description = ''
        Extra Python module packages (each registering the `nixadmin.modules`
        entry point). They are added to the daemon's Python environment and
        discovered automatically.
      '';
    };

    ollama.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Provision a Vulkan Ollama container (active only when local.model is set).";
    };

    ollama.vulkanIcd = lib.mkOption {
      type = lib.types.str;
      default = "/run/opengl-driver/share/vulkan/icd.d/radeon_icd.x86_64.json";
      description = "Vulkan ICD file inside the container (default: AMD RADV).";
    };

    tray.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the system-tray client (a StatusNotifierItem): a quiet dot that goes
        amber when a service fails, with one-click fix-its. Needs a desktop with
        an SNI tray host (KDE, or COSMIC's status-area applet).
      '';
    };

    web.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the detail web view: a loopback-only, token-gated page (the tray's
        "Open detail" link opens it) showing failed units, journals, and per-unit
        restart/explain. Bound to 127.0.0.1; never exposed off the machine.
      '';
    };

    web.port = lib.mkOption {
      type = lib.types.port;
      default = 7677;
      description = "Loopback port for the detail web view.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.groups.nixadmin = { };
    users.users.${cfg.user}.extraGroups = [ "nixadmin" ]
      ++ lib.optionals ollamaEnabled [ "render" "video" ];

    environment.systemPackages = [ pkg nixadminApps ];

    virtualisation.podman.enable = lib.mkIf ollamaEnabled true;

    # Linger so the user services start at boot without an interactive session.
    systemd.tmpfiles.rules = [
      "f /var/lib/systemd/linger/${cfg.user} 0644 root root -"
    ];

    # Privileged rebuild helper (root). Owns root:nixadmin 0660 socket itself.
    systemd.services.nixadmin-helper = {
      description = "nixadmin privileged rebuild helper";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];
      environment = {
        NIXADMIN_FLAKE_DIR = cfg.flakeDir;
        NIXADMIN_HOSTNAME = cfg.hostname;
      };
      serviceConfig = {
        Type = "simple";
        User = "root";
        ExecStart = "${helper}/bin/nixadmin-helper";
        Restart = "on-failure";
        RestartSec = "3s";
        KillMode = "process"; # don't kill the nixos-rebuild it spawns
      };
    };

    # The daemon — a systemd USER service (XDG socket, session bus, rootless Ollama).
    systemd.user.services.nixadmin-daemon = {
      description = "nixadmin ambient intelligence daemon";
      after = [ "graphical-session.target" ];
      wantedBy = [ "default.target" ];
      environment = daemonEnv;
      serviceConfig = {
        Type = "simple";
        ExecStart = "${daemonPython}/bin/nixadmin-daemon";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    # The tray — a USER service tied to the graphical session (it needs the
    # session bus and the desktop's SNI tray host). It reconnects on its own if
    # the daemon restarts and re-registers if the tray host restarts.
    systemd.user.services.nixadmin-tray = lib.mkIf cfg.tray.enable {
      description = "nixadmin system-tray client";
      after = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];
      # xdg-open (xdg-utils) for the "Open detail" link; a user service has a
      # minimal PATH otherwise.
      path = [ pkgs.xdg-utils ];
      serviceConfig = {
        Type = "simple";
        ExecStart = "${clientPython}/bin/nixadmin-tray";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    # The detail web view — a loopback-only, token-gated user service. Started
    # with the session; the tray's "Open detail" opens the URL it publishes.
    systemd.user.services.nixadmin-web = lib.mkIf cfg.web.enable {
      description = "nixadmin detail web view (loopback)";
      after = [ "graphical-session.target" ];
      wantedBy = [ "default.target" ];
      environment = { NIXADMIN_WEB_PORT = toString cfg.web.port; };
      serviceConfig = {
        Type = "simple";
        ExecStart = "${clientPython}/bin/nixadmin-web";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    # Rootless Podman Ollama (Vulkan). Only when a local model is configured.
    systemd.user.services.nixadmin-ollama = lib.mkIf ollamaEnabled {
      description = "nixadmin Ollama inference (rootless Podman, Vulkan)";
      after = [ "graphical-session.target" ];
      wantedBy = [ "graphical-session.target" ];
      serviceConfig = {
        Type = "simple";
        Environment = "PATH=/run/wrappers/bin:/run/current-system/sw/bin";
        ExecStartPre = [
          "${ollamaLoad}"
          "-${pkgs.podman}/bin/podman rm -f nixadmin-ollama"
        ];
        ExecStart = "${ollamaStart}";
        ExecStop = "${pkgs.podman}/bin/podman stop nixadmin-ollama";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };

    # No boot preload: the model loads on demand (first query) and unloads after
    # local.keepAlive idle, so an idle machine keeps its RAM/VRAM. The daemon shows
    # a "warming up" status and classifies through the cold load.
  };
}
