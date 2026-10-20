import tempfile
import unittest
from pathlib import Path
from unittest import mock

from binforge.b2r2_validator import validate_binary_paths
from scripts.validate_binaries import infer_architecture


class B2R2ValidationTests(unittest.TestCase):
    def test_infers_every_configured_architecture(self):
        for arch in (
            "x86_32", "x86_64", "arm_32", "arm_64", "mips_32", "mips_64",
            "mipseb_32", "mipseb_64", "ppceb_32", "ppceb_64", "ppc_64",
        ):
            with self.subTest(arch=arch):
                path = Path(f"hello_gcc-13.3.0_{arch}_O2_hello")
                self.assertEqual(infer_architecture(path), arch)

    def test_does_not_guess_architecture_without_matrix_token(self):
        self.assertIsNone(infer_architecture(Path("hello-x86_64")))

    def test_framework_validator_passes_paths_to_pinned_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = Path(temporary) / "validator.dll"
            validator.touch()
            completed = mock.Mock(
                returncode=0,
                stdout='{"checkedFiles":2,"invalidFiles":0}',
                stderr="",
            )
            with mock.patch(
                "binforge.b2r2_validator.subprocess.run", return_value=completed
            ) as run:
                self.assertTrue(validate_binary_paths(
                    ["/tmp/first", "/tmp/second"], "arm_64",
                    dotnet="/dotnet", validator=validator,
                ))
        self.assertEqual(run.call_args.args[0], ["/dotnet", str(validator), "arm_64"])
        self.assertEqual(run.call_args.kwargs["input"], "/tmp/first\n/tmp/second\n")

    def test_framework_validator_rejects_b2r2_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = Path(temporary) / "validator.dll"
            validator.touch()
            completed = mock.Mock(
                returncode=1,
                stdout='{"checkedFiles":1,"invalidFiles":1}',
                stderr='{"path":"/tmp/first","expectedArch":"arm_64"}',
            )
            with mock.patch(
                "binforge.b2r2_validator.subprocess.run", return_value=completed
            ):
                self.assertFalse(validate_binary_paths(
                    ["/tmp/first"], "arm_64",
                    dotnet="/dotnet", validator=validator,
                ))


if __name__ == "__main__":
    unittest.main()
