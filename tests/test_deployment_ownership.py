"""Regression checks for explicit ingress ownership and retry convergence."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class DeploymentOwnershipTests(unittest.TestCase):
    def test_public_ingress_is_not_implicit_for_control_role(self):
        playbook = (ROOT / "ansible/playbook.yml").read_text()
        self.assertIn("OCPP_REPORTS_NGINX_ENABLED", playbook)
        self.assertIn("default('false', true) | bool", playbook)
        self.assertIn("OCPP_REPORTS_DIAGNOSTICS_NGINX_ENABLED", playbook)

    def test_dns_is_not_proof_of_diagnostic_ownership(self):
        tasks = (ROOT / "ansible/tasks/nginx.yml").read_text()
        self.assertNotIn("reports_diagnostics_dns.rc", tasks)
        self.assertNotIn("reports_diagnostics_auto_cert.stat.exists", tasks)
        self.assertIn("reports_diagnostics_nginx_enabled | bool", tasks)
        self.assertIn("- reports_diagnostics_nginx_enabled | bool", tasks)

    def test_linked_release_never_short_circuits_role_convergence(self):
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertNotIn("status --json", deploy)
        self.assertNotIn("skipped Ansible", deploy)
        self.assertIn("exec ansible-playbook", deploy)


if __name__ == "__main__":
    unittest.main()
