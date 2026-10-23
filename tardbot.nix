# NixOS module for tardBot.
#   services.tardbot.enable = true;
#   services.tardbot.environmentFile = "/etc/tardbot.env";   # contains DISCORD_TOKEN=...
{ config, lib, pkgs, ... }:
let
  cfg = config.services.tardbot;
  py = pkgs.python3;
  src = lib.cleanSource ./.;
  state = "/var/lib/tardbot";
in
{
  options.services.tardbot = {
    enable = lib.mkEnableOption "tardBot Discord bot";

    environmentFile = lib.mkOption {
      type = lib.types.path;
      default = "/etc/tardbot.env";
      description = "File with secrets, at least DISCORD_TOKEN=...";
    };

    modules = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "music" "general" ];
      description = "Modules to load. Empty = every module folder found.";
    };

    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = { INTERLEAVE_MODE = "spread"; };
      description = "Extra environment variables (module settings).";
    };

    updateSchedule = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "Sun 04:00";
      description = "When to restart the bot so it picks up a new yt-dlp. null disables it.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Static user instead of DynamicUser: DynamicUser mounts the state dir noexec,
    # which breaks pip-installed native wheels.
    users.users.tardbot = {
      isSystemUser = true;
      group = "tardbot";
      home = state;
    };
    users.groups.tardbot = { };

    systemd.services.tardbot = {
      description = "tardBot (Discord)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      # ffmpeg for audio, deno for yt-dlp's YouTube challenge solving (music module)
      path = [ pkgs.ffmpeg pkgs.deno ];

      environment = {
        HOME = state;
        PYTHONDONTWRITEBYTECODE = "1";
        OPUS_LIB = "${pkgs.libopus}/lib/libopus.so.0";
        # runtime libs that pip wheels (davey, PyNaCl) expect
        LD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib pkgs.libopus ];
        EMPTY_VC_GRACE_SECONDS = "10";
        INTERLEAVE_MODE = "alternate"; # or "spread"
      }
      // lib.optionalAttrs (cfg.modules != [ ]) { TARDBOT_MODULES = lib.concatStringsSep "," cfg.modules; }
      // cfg.settings;

      serviceConfig = {
        User = "tardbot";
        Group = "tardbot";
        StateDirectory = "tardbot";
        WorkingDirectory = state;
        EnvironmentFile = cfg.environmentFile;

        ExecStartPre = pkgs.writeShellScript "tardbot-venv" ''
          set -eu
          if ! .venv/bin/python -c "" 2>/dev/null; then
            rm -rf .venv
            ${py}/bin/python -m venv .venv
          fi
          .venv/bin/python -m pip install --quiet -r ${src}/requirements.txt
          # yt-dlp breaks often: grab the newest on every start (don't fail if offline)
          .venv/bin/python -m pip install --quiet -U "yt-dlp[default]" || true
        '';
        ExecStart = "${state}/.venv/bin/python ${src}/index.py";

        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    # Periodic restart = yt-dlp update
    systemd.services.tardbot-update = lib.mkIf (cfg.updateSchedule != null) {
      description = "Restart tardBot to pick up a new yt-dlp";
      serviceConfig.Type = "oneshot";
      script = "systemctl restart tardbot.service";
      startAt = cfg.updateSchedule;
    };
  };
}
