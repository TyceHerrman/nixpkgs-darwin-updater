import subprocess
import tempfile
import unittest
from pathlib import Path

FAKE_NIXPKGS = r"""
{}:
let
  make = name: builtins.derivation {
    inherit name;
    system = builtins.currentSystem;
    builder = "/bin/sh";
    args = [ "-c" "touch $out" ];
  };
  lib = rec {
    splitString = separator: value: [ value ];
    attrByPath = path: default: value:
      if path == [] then value
      else
        let
          name = builtins.head path;
        in
        if builtins.hasAttr name value then
          attrByPath (builtins.tail path) default (builtins.getAttr name value)
        else
          default;
    isDerivation = value:
      builtins.isAttrs value && (value.type or null) == "derivation";
    collect = predicate: value:
      if predicate value then [ value ]
      else if builtins.isAttrs value then
        builtins.concatLists (
          map (name: collect predicate (builtins.getAttr name value))
            (builtins.attrNames value)
        )
      else
        [];
  };
in
{
  inherit lib;
  example-app = (make "example-app") // {
    tests = {
      one = make "example-test-one";
      nested = {
        ignored = "not a derivation";
        two = make "example-test-two";
      };
    };
  };
}
"""


class PassthruTestsExpressionTests(unittest.TestCase):
    def test_discovers_nested_derivation_valued_passthru_tests(self):
        expression = Path(__file__).with_name("passthru-tests.nix")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "default.nix"
            fixture.write_text(FAKE_NIXPKGS)

            result = subprocess.run(
                [
                    "nix-instantiate",
                    str(expression),
                    "--arg",
                    "nixpkgs",
                    str(fixture.parent),
                    "--argstr",
                    "package",
                    "example-app",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        derivations = result.stdout.splitlines()
        self.assertEqual(len(derivations), 2)
        self.assertTrue(any("example-test-one.drv" in path for path in derivations))
        self.assertTrue(any("example-test-two.drv" in path for path in derivations))


if __name__ == "__main__":
    unittest.main()
