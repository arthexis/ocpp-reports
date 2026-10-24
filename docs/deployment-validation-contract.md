# Deployment validation contract

This is a cross-repository **behavioral contract**, not a shared implementation.
Each repository may keep its own CLI, Ansible layout, installation strategy,
systemd units and runtime health implementation.

## Test boundaries

1. **Unit/contract tests:** deterministic and isolated. A failing injected
   dependency must not silently fall through to the runner's real host state.
   Inject or fixture every consulted source of evidence, including independent
   release-state readers. Assert documented semantics rather than a particular
   Ansible file path, playbook shape or service implementation.
2. **Integration tests:** exercise the real supported installer and public
   interfaces in an isolated, disposable environment. Do not inspect or mutate
   the runner's unrelated live deployment. Keep any exceptional host-level test
   explicitly scoped and documented.
3. **Field/deployment validation:** before activation, verify the intended
   immutable revision and prerequisites. After activation, report installed
   revision and actual runtime health using authoritative evidence, not a
   previous deployment attempt or an inferred success.
4. **Evidence semantics:** distinguish attempted, staged, installed/activated
   and running/healthy. Do not claim that installation implies health or that
   an accepted request implies completion. If evidence is unavailable, use
   unknown/not-applicable as appropriate; never replace authoritative independent
   evidence with the injected status of another subsystem.
5. **Failure handling:** fail closed before changing the live installation when
   validation fails. Include actionable, sanitized errors; do not leak tokens,
   secrets or private configuration. Preserve each repository's existing
   fail-forward and rollback boundary without imposing another repo's strategy.
6. **CI gate:** PR CI is the authoritative correctness gate. A post-merge
   deployment workflow may verify its target revision, preflight and live
   health; it must not be interpreted as an alternative to green PR CI.
   Do not add post-merge main test CI merely to satisfy this contract.

## Required test coverage

- Inject a failing health/status dependency and confirm JSON/structured output
  is serializable and state remains truthful.
- Inject independent release evidence separately from a failing probe and
  verify it is preserved, or explicitly test the equivalent repository-specific
  evidence separation.
- Confirm missing/invalid evidence does not become a fabricated successful
  installation or healthy status.
- Confirm failure of preflight prevents activation; exercise the supported
  installer in isolated integration tests where the repository has a deployer.

This document defines *observable behavior only*. Do not hardcode the file
names, exact state vocabulary, probe implementations, service names, or CLI
parameters of another repository in its tests.
