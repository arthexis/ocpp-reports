"""Regression checks for opt-in certificate authentication at the dashboard edge."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "ansible/templates/ocpp-reports-dashboard-instance.nginx.j2"
TASKS = ROOT / "ansible/tasks/dashboard-instances.yml"


class DashboardMtlsTests(unittest.TestCase):
    def test_basic_auth_remains_available(self):
        template = TEMPLATE.read_text()
        self.assertIn('auth_basic "{{ item.title }}"', template)
        self.assertIn("auth_basic_user_file {{ item.htpasswd }};", template)
        self.assertIn("satisfy any;", template)
        self.assertIn("auth_request /__ocpp_mtls_authorize;", template)

    def test_mtls_is_opt_in_and_fails_closed(self):
        template = TEMPLATE.read_text()
        self.assertIn("{% if item.mtls_ca_file is defined %}", template)
        self.assertIn("ssl_verify_client optional;", template)
        self.assertIn("if ($ssl_client_verify = SUCCESS) { return 204; }", template)
        self.assertIn("return 403;", template)
        self.assertIn("internal;", template)

    def test_trust_anchor_must_exist(self):
        tasks = TASKS.read_text()
        self.assertIn("Check optional dashboard mTLS trust anchors", tasks)
        self.assertIn("Refuse missing dashboard mTLS trust anchors", tasks)
        self.assertIn("item.mtls_ca_file is not defined", tasks)


if __name__ == "__main__":
    unittest.main()
