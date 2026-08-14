{
  description = "pi-xorq verification duel — side-by-side demo of verified-by-construction data answers";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3;
        in
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
            ]
          )
      );

      piPackages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          nodejs = pkgs.nodejs_22;
        in
        pkgs.buildNpmPackage {
          pname = "pi";
          version = "0.84.1";

          src = ./nix/pi;
          npmDepsHash = "sha256-ujxUWXY/7bEdkj4IXsTys7SycyY2XiktOhe/AaLz4V4=";

          inherit nodejs;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          dontNpmBuild = true;
          makeCacheWritable = true;

          installPhase = ''
            runHook preInstall

            mkdir -p $out/lib/pi-nix $out/bin
            cp -R . $out/lib/pi-nix
            makeWrapper ${nodejs}/bin/node $out/bin/pi \
              --add-flags "$out/lib/pi-nix/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"

            runHook postInstall
          '';

          meta = {
            description = "Pi coding agent CLI";
            mainProgram = "pi";
          };
        }
      );

    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = pythonSet.mkVirtualEnv "pi-xorq-duel-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              piPackages.${system}
              pkgs.uv
              # duel.sh drives the three panes with tmux
              pkgs.tmux
              # record.sh captures a cast of the duel; agg renders it to a gif,
              # gifsicle requantizes that gif down to README weight
              pkgs.asciinema
              pkgs.asciinema-agg
              pkgs.gifsicle
              # Shadow Apple's /usr/bin/git shim: with DEVELOPER_DIR pointing into
              # the nix apple-sdk it warns "unhandled Platform key FamilyDisplayName"
              # on every invocation (3x per xorq catalog command, which shells to git).
              pkgs.git
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              # Apple's /usr/bin xcrun shims (git among them) warn "unhandled
              # Platform key FamilyDisplayName" whenever DEVELOPER_DIR/SDKROOT
              # point into the nix apple-sdk — and a login subshell (pi's bash
              # tool) lets macOS path_helper put /usr/bin back ahead of the nix
              # git above, so every `xorq catalog` call dumped three warning
              # lines into the agent's context. Nothing in this shell compiles
              # at runtime; drop the SDK pins so the shims stay quiet wherever
              # PATH resolution lands.
              unset DEVELOPER_DIR SDKROOT
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );

      formatter = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        pkgs.nixfmt-tree
      );
      packages = forAllSystems (system: {
        default = pythonSets.${system}.mkVirtualEnv "pi-xorq-duel-env" workspace.deps.default;
        pi = piPackages.${system};
      });
    };
}
