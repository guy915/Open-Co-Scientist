import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import call, patch

from scripts.ci.container_smoke import smoke, wait_ready


class ContainerSmokeTests(unittest.TestCase):
    def test_completed_variants_release_their_layer_before_the_next_start(self):
        active = set()

        def docker(*args, **kwargs):
            if args[0] == "run":
                if active:
                    raise subprocess.CalledProcessError(
                        125, ["docker", *args], stderr="container layer budget exceeded"
                    )
                active.add(args[args.index("--name") + 1])
            elif args[:2] == ("rm", "-f"):
                active.discard(args[2])
            return ""

        with (
            patch("scripts.ci.container_smoke.docker", side_effect=docker),
            patch("scripts.ci.container_smoke.wait_ready") as ready,
        ):
            smoke("api-image", "mcp-image")
        self.assertEqual(ready.call_count, 3)
        self.assertFalse(active)

    def test_launch_failure_reports_cli_error_logs_and_cleans_resources(self):
        def docker(*args, **kwargs):
            if args[0] == "run":
                raise subprocess.CalledProcessError(
                    125, ["docker", *args], stderr="no space left on device"
                )
            return ""

        stderr = io.StringIO()
        with (
            patch("scripts.ci.container_smoke.docker", side_effect=docker) as run,
            redirect_stderr(stderr),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                smoke("api-image", "mcp-image")
        self.assertIn("no space left on device", stderr.getvalue())
        self.assertTrue(any(item.args[0] == "logs" for item in run.call_args_list))
        self.assertTrue(
            any(item.args[:2] == ("rm", "-f") for item in run.call_args_list)
        )
        self.assertTrue(
            any(item.args[:2] == ("volume", "rm") for item in run.call_args_list)
        )

    def test_lifespan_failure_fails_and_prints_logs(self):
        def docker(*args, **kwargs):
            if args[:2] == ("inspect", "--format"):
                return "false"
            return "container output"

        with patch("scripts.ci.container_smoke.docker", side_effect=docker) as run:
            with self.assertRaisesRegex(RuntimeError, "exited"):
                wait_ready("api", "/health", 8008, 60)
            self.assertIn(call("logs", "api", capture=False), run.call_args_list)

    def test_timeout_is_bounded_and_prints_logs(self):
        with (
            patch("scripts.ci.container_smoke.time.monotonic", side_effect=[0, 61]),
            patch("scripts.ci.container_smoke.docker") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "60"):
                wait_ready("api", "/health", 8008, 60)
            run.assert_called_with("logs", "api", capture=False)

    def test_failed_probes_can_pass_before_deadline(self):
        probes = 0

        def docker(*args, **kwargs):
            nonlocal probes
            if args[0] == "inspect":
                return "true"
            if args[0] == "exec":
                probes += 1
                if probes == 1:
                    raise subprocess.CalledProcessError(1, ["docker", *args])
            return ""

        with (
            patch("scripts.ci.container_smoke.docker", side_effect=docker),
            patch("scripts.ci.container_smoke.time.sleep"),
        ):
            wait_ready("api", "/health", 8008, 60)
            self.assertEqual(probes, 2)

    def test_root_empty_volume_and_image_user_start_with_real_entrypoint(self):
        with (
            patch("scripts.ci.container_smoke.docker", return_value="") as run,
            patch("scripts.ci.container_smoke.wait_ready") as ready,
        ):
            smoke("api-image", "mcp-image")
            starts = [call.args for call in run.call_args_list if call.args[0] == "run"]
            self.assertEqual(len(starts), 3)
            self.assertIn("0", starts[0])
            self.assertTrue(any("volume-nocopy" in arg for arg in starts[0]))
            self.assertNotIn("--user", starts[1])
            self.assertNotIn("--mount", starts[1])
            for args in starts[:2]:
                self.assertIn("COSCIENTIST_TEST_DOUBLE=deterministic", args)
                self.assertIn("COSCIENTIST_TRUSTED_PROXY_CIDRS=127.0.0.1/32", args)
            for args in starts:
                self.assertIn("none", args)
                self.assertNotIn("--entrypoint", args)
            self.assertEqual(ready.call_count, 3)

    def test_probe_failure_cleans_every_container_and_volume(self):
        with (
            patch("scripts.ci.container_smoke.docker", return_value="") as run,
            patch(
                "scripts.ci.container_smoke.wait_ready",
                side_effect=RuntimeError("unhealthy"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                smoke("api-image", "mcp-image")
            self.assertTrue(
                any(call.args[:2] == ("rm", "-f") for call in run.call_args_list)
            )
            self.assertTrue(
                any(call.args[:2] == ("volume", "rm") for call in run.call_args_list)
            )


if __name__ == "__main__":
    unittest.main()
