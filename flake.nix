{
  description = "tardBot - modular Discord bot";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs, ... }: {
    nixosModules.tardbot = import ./tardbot.nix;
    nixosModules.default = self.nixosModules.tardbot;
  };
}
