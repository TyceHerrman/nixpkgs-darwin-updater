{ nixpkgs, package }:
let
  pkgs = import nixpkgs { };
  target = pkgs.lib.attrByPath (pkgs.lib.splitString "." package) (
    throw "package ${package} does not exist"
  ) pkgs;
  tests = target.tests or { };
in
pkgs.lib.collect pkgs.lib.isDerivation tests
