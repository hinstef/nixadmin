self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.nixadmin;
  pkg = self.packages.${pkgs.system}.nixadmin;

  # Privileged rebuild helper — runs as root, owns a group-accessible socket.
  helper = pkgs.writers.writePython3Bin "nixadmin-helper" { } (builtins.readFile ./nixadmin-helper.py);

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
    NIXADMIN_LOG_FORMAT = cfg.logFormat;
  };
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

    logFormat = lib.mkOption {
      type = lib.types.enum [ "json" "console" ];
      default = "json";
      description = "Daemon log renderer.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.groups.nixadmin = { };
    users.users.${cfg.user}.extraGroups = [ "nixadmin" ];

    environment.systemPackages = [ pkg ];

    # Linger so the user daemon starts at boot without an interactive session.
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
        # Only kill the helper itself, not the nixos-rebuild it spawns.
        KillMode = "process";
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
        ExecStart = "${pkg}/bin/nixadmin-daemon";
        Restart = "on-failure";
        RestartSec = "5s";
      };
    };
  };
}
