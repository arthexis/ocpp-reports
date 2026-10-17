from pathlib import Path


def test_deploy_entrypoint():
    root = Path(__file__).resolve().parents[1]
    assert 'ansible-playbook' in (root / 'deploy.sh').read_text()
    assert 'git rev-parse HEAD' in (root / 'ansible/playbook.yml').read_text()
    assert 'ocpp-reports status --json' in (root / 'ansible/playbook.yml').read_text()
