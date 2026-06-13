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
        # buildPythonPackage (not Application) so it composes in a python env
        # alongside third-party module packages for entry-point discovery.
        nixadmin = pkgs.python3.pkgs.buildPythonPackage {
          pname = "nixadmin";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3.pkgs.hatchling ];
          dependencies = with pkgs.python3.pkgs; [ httpx litellm dbus-fast structlog ];
          doCheck = false; # tests run via `nix flake check`
        };

        # Reference third-party module package — discovered via entry points.
        nixadmin-extras = pkgs.python3.pkgs.buildPythonPackage {
          pname = "nixadmin-extras";
          version = "0.1.0";
          src = ./contrib/nixadmin-extras;
          pyproject = true;
          build-system = [ pkgs.python3.pkgs.hatchling ];
          dependencies = [ nixadmin ];
          doCheck = false;
        };

        default = nixadmin;
      });

      devShells = forAll (system: pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              pytest pytest-asyncio structlog httpx litellm dbus-fast mypy
            ]))
            pkgs.ruff
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            echo "nixadmin dev shell — run: pytest -q  ·  ruff check .  ·  mypy src/nixadmin"
          '';
        };
      });

      # `nix flake check` enforces all three gates in the sandbox.
      checks = forAll (system: pkgs:
        let
          pyEnv = pkgs.python3.withPackages (ps: with ps; [
            pytest pytest-asyncio structlog httpx litellm dbus-fast mypy
          ]);
          check = name: deps: cmd: pkgs.runCommand "nixadmin-${name}"
            { nativeBuildInputs = deps; }
            ''
              cp -r ${./.} src-tree && chmod -R +w src-tree && cd src-tree
              export PYTHONPATH="$PWD/src"
              ${cmd}
              touch $out
            '';
        in
        {
          pytest = check "pytest" [ pyEnv ] "pytest -q";
          mypy = check "mypy" [ pyEnv ] "mypy src/nixadmin";
          ruff = check "ruff" [ pkgs.ruff ] "ruff check src tests contrib";
        });

      nixosModules.default = import ./nix/module.nix self;
    };
}
