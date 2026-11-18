{
  description = "wakeful - keep a KDE Plasma session awake for a set time";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        # Version is updated by the release workflow before tagging
        version = "0.2.0";
        wakeful = pkgs.callPackage ./nix/package.nix {
          inherit version;
          src = self;
        };
      in
      {
        packages = {
          default = wakeful;
          inherit wakeful;
        };
        checks = {
          default = wakeful;
          # Evaluates the NixOS module and keeps the unit it generates.
          nixos-module = pkgs.writeText "wakeful-user-unit" (
            (nixpkgs.lib.nixosSystem {
              inherit system;
              modules = [
                self.nixosModules.default
                {
                  programs.wakeful.enable = true;
                  boot.loader.grub.enable = false;
                  fileSystems."/".device = "none";
                  system.stateVersion = "25.11";
                }
              ];
            }).config.systemd.user.units."wakeful.service".text
          );
        };
      }
    )
    // {
      homeManagerModules.default = import ./nix/home-manager.nix self;
      nixosModules.default = import ./nix/nixos.nix self;
    };
}
