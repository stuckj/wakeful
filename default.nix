{
  pkgs ? import <nixpkgs> { },
}:

pkgs.callPackage ./nix/package.nix {
  # Version is updated by the release workflow before tagging
  version = "0.2.0";
  src = ./.;
}
