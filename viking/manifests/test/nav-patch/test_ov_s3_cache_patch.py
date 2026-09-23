"""Guard and option-preservation checks; the S3 race is a separate live test."""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ov_s3_cache_patch as cache_patch


class CachePatchTests(unittest.TestCase):
    def test_preserves_parameters_without_mutating_original_result(self):
        params = {
            "bucket": "test",
            "directory_marker_mode": "empty",
            "cache_enabled": True,
        }
        wrapped = cache_patch.without_metadata_cache(lambda config: params)
        self.assertEqual(wrapped(None), {**params, "cache_enabled": False})
        self.assertTrue(params["cache_enabled"])

    def test_propagates_original_errors(self):
        def fail(config):
            raise ValueError("invalid config")

        with self.assertRaisesRegex(ValueError, "invalid config"):
            cache_patch.without_metadata_cache(fail)(None)

    def test_guards_and_idempotence(self):
        original = lambda cfg: {"bucket": "test"}
        module = types.SimpleNamespace(_serialize_s3_plugin_params=original)
        ov = types.SimpleNamespace(__version__="v0.4.99")
        with (
            patch.dict(sys.modules, {"openviking": ov}),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertFalse(cache_patch.apply(module))
            ov.__version__ = cache_patch.EXPECTED_VERSION
            self.assertFalse(cache_patch.apply(module))
            with patch.object(
                cache_patch,
                "EXPECTED_SHA256",
                cache_patch.hashlib.sha256(
                    cache_patch.inspect.getsource(original).encode()
                ).hexdigest(),
            ):
                with patch.dict(os.environ, {"OV_S3_CACHE_PATCH": "0"}):
                    self.assertFalse(cache_patch.apply(module))
                self.assertTrue(cache_patch.apply(module))
                installed = module._serialize_s3_plugin_params
                self.assertTrue(cache_patch.apply(module))
                self.assertIs(module._serialize_s3_plugin_params, installed)
                self.assertFalse(installed(None)["cache_enabled"])


if __name__ == "__main__":
    unittest.main()
