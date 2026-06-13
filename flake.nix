{
  description = "nixadmin — ambient system intelligence daemon for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (system: pkgs: rec {
        nixadmin = pkgs.python3.pkgs.buildPythonApplication {
          pname = "nixadmin";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3.pkgs.hatchling ];
          dependencies = with pkgs.python3.pkgs; [ httpx litellm dbus-fast structlog ];
          # The smoke tests run in the dev shell; keep the build lean.
          doCheck = false;
        };
        default = nixadmin;
      });

      devShells = forAll (system: pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              pytest pytest-asyncio structlog httpx litellm dbus-fast
            ]))
            pkgs.ruff
            pkgs.mypy
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            echo "nixadmin dev shell — run: pytest -q"
          '';
        };
      });

      nixosModules.default = import ./nix/module.nix self;
    };
}
