"""Hermetic contract for pinned local Reports wheel builds."""
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1] / "ansible/tasks/release.yml"


def test_wheel_build_uses_local_checkout_not_recursive_ansible_staging():
    source = RELEASE.read_text()
    assert "Stage pinned Reports package files" not in source
    assert 'dest: "{{ reports_release }}/source/{{ item }}"' not in source
    build = source.split("- name: Build Reports application wheel", 1)[1].split(
        "- name: Find built Reports wheel", 1
    )[0]
    assert '- "git+file://{{ playbook_dir }}/..@{{ reports_commit }}"' in build
    assert "become: true" in build
    assert '- "{{ playbook_dir }}/.."' not in build.split("- name: Verify pinned Reports checkout", 1)[0]
    assert "- --no-deps" in build
    assert "- --no-build-isolation" in build


def test_revision_label_and_package_content_are_guarded_around_wheel_build():
    source = RELEASE.read_text()
    before = source.index("Verify pinned Reports checkout has no local package edits")
    build = source.index("Build Reports application wheel")
    after = source.index("Verify pinned Reports checkout remained unchanged during wheel build")
    install = source.index("Install Reports wheel without dependency downloads")
    assert before < build < after < install
    assert "reports_build_checkout_revision.stdout | trim) != reports_commit" in source
    assert "reports_postbuild_revision.stdout | trim) != reports_commit" in source
    assert source.count("- --untracked-files=all") == 2


def test_release_activation_is_still_separate_from_build():
    source = (RELEASE.parent / "../playbook.yml").resolve().read_text()
    assert "tasks/release.yml" in source
    assert "tasks/activation.yml" in source
    assert source.index("tasks/release.yml") < source.index("tasks/activation.yml")


def test_local_pinned_git_build_cannot_create_root_owned_checkout_artifacts():
    source = RELEASE.read_text()
    build = source.split("- name: Build Reports application wheel", 1)[1].split(
        "- name: Find built Reports wheel", 1
    )[0]
    assert "git+file://" in build and "@{{ reports_commit }}" in build
    assert 'become_user: "{{ reports_operator }}"' in build
    assert '"{{ reports_wheel_temp.path }}"' in build
    assert "Promote single Reports wheel to immutable release" in source
    assert "remote_src: true" in source
    assert "Clean temporary Reports wheel directory" in source
    assert "always:" in source
    integration = (RELEASE.parents[2] / "scripts/ci_deploy_integration.sh").read_text()
    assert "test ! -d build" in integration
    assert "'*.egg-info'" in integration
