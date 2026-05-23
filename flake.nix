{
  description = "nixadmin — reusable NixOS module for a locally-running AI sysadmin";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }: {
    nixosModules.default  = import ./modules/nixos/nixadmin.nix;
    nixosModules.nixadmin = self.nixosModules.default;
  };
}
