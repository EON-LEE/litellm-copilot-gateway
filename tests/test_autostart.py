"""Windows autostart: the scheduled task only manages the default instance, and delegation never spawns."""
import os
from pathlib import Path
import unittest
from unittest import mock

from helpers import TempHome

from copilot_gateway import autostart, gateway, services
from copilot_gateway.settings import Settings


class AutostartTests(unittest.TestCase):
    def test_only_the_default_instance_is_scheduled(self):
        home = TempHome()
        self.addCleanup(home.cleanup)
        with mock.patch.object(autostart, "WINDOWS", True):
            self.assertFalse(autostart.serves(home.settings()))
            plain = {k: v for k, v in os.environ.items() if k not in autostart.OVERRIDE_VARS}
            self.assertTrue(autostart.serves(Settings.load(plain)))
        built = gateway.build_services(home.settings(), key="k")
        self.assertTrue(all(service.scheduler is None for service in built.values()))

    def test_task_runs_one_instance_on_battery_too(self):
        with mock.patch.object(autostart, "task_command", return_value=(r"C:\a&b\pythonw.exe", "-m x")):
            xml = autostart.task_xml()
        self.assertIn(r"<Command>C:\a&amp;b\pythonw.exe</Command>", xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml)
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", xml)

    def test_delegated_start_waits_for_the_task_instead_of_spawning(self):
        scheduler = mock.Mock(TASK="t")
        scheduler.delegate.return_value = True
        command = mock.Mock()
        service = services.Service("codex", 1, Path("missing.log"), "litellm", command=command,
                                   startup_timeout=1, scheduler=scheduler)
        with mock.patch.object(services, "inspect", return_value=services.State("stopped", [])), \
                mock.patch.object(services, "port_open", return_value=False):
            with self.assertRaisesRegex(services.ServiceError, "scheduled task t"):
                services.start(service, log=lambda *_: None)
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
