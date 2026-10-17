{
  description = "Discord Music Bot Module";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, ... }: {
    nixosModules.musicbot = import ./musicbot.nix;
  };
}