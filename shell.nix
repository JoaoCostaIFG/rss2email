{ nixpkgs ? import ./nix/nixpkgs.nix }:
let
  pkgs = import nixpkgs {};
in pkgs.mkShell {
  buildInputs = with pkgs; [
    uv
    python313
    git
  ];
}