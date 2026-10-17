# NixOS module for the Discord music bot.
{ config, lib, pkgs, ... }:
let
  py = pkgs.python3;
  src = ./.;
  state = "/var/lib/musicbot";
in
{
  # Static user instead of DynamicUser: DynamicUser mounts the state dir noexec,
  # which breaks pip-installed native wheels.
  users.users.musicbot = {
    isSystemUser = true;
    group = "musicbot";
    home = state;
  };
  users.groups.musicbot = { };

  systemd.services.musicbot = {
    description = "Discord music bot";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];

    # ffmpeg for audio, deno for yt-dlp's YouTube challenge solving
    path = [ pkgs.ffmpeg pkgs.deno ];

    environment = {
      HOME = state;
      PYTHONDONTWRITEBYTECODE = "1";
      OPUS_LIB = "${pkgs.libopus}/lib/libopus.so.0";
      # runtime libs that pip wheels (davey, PyNaCl) expect
      LD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib pkgs.libopus ];
      EMPTY_VC_GRACE_SECONDS = "10";
      INTERLEAVE_MODE = "alternate"; # or "spread"
    };

    serviceConfig = {
      User = "musicbot";
      Group = "musicbot";
      StateDirectory = "musicbot";
      WorkingDirectory = state;
      # contains: DISCORD_TOKEN=...
      EnvironmentFile = "/etc/musicbot.env";

      ExecStartPre = pkgs.writeShellScript "musicbot-venv" ''
        set -eu
        if ! .venv/bin/python -c "" 2>/dev/null; then
          rm -rf .venv
          ${py}/bin/python -m venv .venv
        fi
        .venv/bin/python -m pip install --quiet -r ${src}/requirements.txt
        # yt-dlp breaks often: grab the newest on every start (don't fail if offline)
        .venv/bin/python -m pip install --quiet -U "yt-dlp[default]" || true
      '';
      ExecStart = "${state}/.venv/bin/python ${src}/bot.py";

      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  # Weekly restart = weekly yt-dlp update
  systemd.services.musicbot-update = {
    description = "Restart music bot to pick up a new yt-dlp";
    serviceConfig.Type = "oneshot";
    script = "systemctl restart musicbot.service";
    startAt = "Sun 04:00";
  };
}