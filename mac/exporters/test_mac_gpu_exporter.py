#!/usr/bin/env python3
"""Unit tests for the powermetrics parser."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

# Import via importlib to handle the hyphenated filename.
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "mac_gpu_exporter",
    os.path.join(os.path.dirname(__file__), "mac-gpu-exporter.py"),
)
mac_gpu_exporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mac_gpu_exporter)
parse_powermetrics = mac_gpu_exporter.parse_powermetrics


NOMINAL_OUTPUT = """\
*** Sampled system activity (Tue Feb 27 11:00:00 2026) (1000.55 ms elapsed) ***

**** Processor usage ****
CPU usage: 12.34%

**** GPU usage ****
GPU HW active frequency: 720 MHz
GPU HW active residency:  42.31% (444 MHz:   1.20% 612 MHz:   2.10% 720 MHz:  39.01%)
GPU SW requested state: (P1 :  10.00% P2 :  30.00% P3 :  60.00%)
GPU SW state: (SW_P1 :  10.00% SW_P2 :  30.00% SW_P3 :  60.00%)
GPU idle residency:  57.69%
GPU Power: 1234 mW

**** ANE usage ****
ANE Power: 250 mW

**** SoC power ****
Combined Power (CPU + GPU + ANE): 5678 mW

**** Thermal pressure ****
Current pressure level: Nominal
"""

CRITICAL_OUTPUT = """\
GPU Power: 9000 mW
GPU HW active residency: 99.50%
GPU HW active frequency: 1278 MHz
ANE Power: 0 mW
Combined Power (CPU + GPU + ANE): 18000 mW
Current pressure level: Critical
"""

MISSING_ANE = """\
GPU Power: 500 mW
GPU HW active residency: 5.00%
GPU HW active frequency: 444 MHz
Combined Power (CPU + GPU + ANE): 2000 mW
Current pressure level: Fair
"""

EMPTY = ""


class TestParsePowermetrics(unittest.TestCase):
    def test_nominal_full_parse(self):
        m = parse_powermetrics(NOMINAL_OUTPUT)
        self.assertAlmostEqual(m["mac_gpu_power_watts"], 1.234)
        self.assertAlmostEqual(m["mac_gpu_utilization_percent"], 42.31)
        self.assertAlmostEqual(m["mac_gpu_freq_mhz"], 720.0)
        self.assertAlmostEqual(m["mac_ane_power_watts"], 0.25)
        self.assertAlmostEqual(m["mac_cpu_package_power_watts"], 5.678)
        self.assertEqual(m["mac_thermal_pressure"], 0)

    def test_critical_thermal(self):
        m = parse_powermetrics(CRITICAL_OUTPUT)
        self.assertEqual(m["mac_thermal_pressure"], 3)
        self.assertAlmostEqual(m["mac_gpu_power_watts"], 9.0)
        self.assertAlmostEqual(m["mac_cpu_package_power_watts"], 18.0)

    def test_fair_thermal_and_missing_ane(self):
        m = parse_powermetrics(MISSING_ANE)
        self.assertEqual(m["mac_thermal_pressure"], 1)
        self.assertAlmostEqual(m["mac_ane_power_watts"], 0.0)
        self.assertAlmostEqual(m["mac_gpu_power_watts"], 0.5)

    def test_empty_input_defaults_to_zero(self):
        m = parse_powermetrics(EMPTY)
        self.assertEqual(m["mac_gpu_power_watts"], 0.0)
        self.assertEqual(m["mac_gpu_utilization_percent"], 0.0)
        self.assertEqual(m["mac_thermal_pressure"], 0)

    def test_unknown_thermal_level_defaults_to_zero(self):
        m = parse_powermetrics("Current pressure level: Bogus\n")
        self.assertEqual(m["mac_thermal_pressure"], 0)


if __name__ == "__main__":
    unittest.main()
