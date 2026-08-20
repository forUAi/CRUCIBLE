# Contributing

## The standard of evidence

CRUCIBLE is a verification tool, so the bar for changing it is the bar it
applies to repositories: **a claim is worth exactly the evidence attached to
it.**

Concretely, for any change that touches containment, resource limits,
networking or lifecycle:

- **Reproduce the problem first.** A fix whose failure was never observed is
  a guess. Several defects in this codebase were found only because a test
  refused to accept a plausible explanation — the concurrent-box zeros were
  blamed on a store collision that turned out not to be the cause.
- **Prove the test would fail without the fix.** A test that passes both ways
  measures nothing.
- **Never let a skipped operation report success.** A snapshot hit, an absent
  corpus or a probe that produced no output must fail loudly.
  `INCONCLUSIVE` and `MEASUREMENT_FAILED` exist for this and are never
  passes.
- **Report the first result.** Do not retry until green. If a failure does
  not reproduce, say so and preserve it.

## Development setup

CRUCIBLE's runtime is standard library only, on Python 3.11+. The analysis
half runs anywhere; execution needs a Linux guest.

```bash
python3 -m venv .venv
.venv/bin/python -m unittest discover -s tests
```

For anything that executes, provision the verification VM and work from the
artifact — see [REPRODUCING.md](REPRODUCING.md).

## Before opening a pull request

```bash
# 1. Unit and artifact-contract tests
python3 -m unittest discover -s tests

# 2. Threat model still resolves
python3 security/tm_check.py

# 3. Whichever suites your change touches, in the guest as root
sudo python3 security/lifecycle_test.py --case all
sudo python3 security/resources.py --case all
sudo python3 security/netmodes.py
sudo python3 security/contain.py --repeat 3 --run hostile-python
```

For a change that affects containment, run the complete gate against a built
artifact rather than the checkout.

## Things the tests enforce

`tests/test_artifact_contract.py` checks properties of the **shipped tree**,
not the checkout, because twice a fix was reported as applied and was not. It
will reject:

- A hardcoded home directory in any source file.
- An execution-benchmark target that is neither shipped in the artifact nor a
  pinned `https` URL with a full 40-character commit SHA.
- A `harness_error` result that discards the real exception type.
- A backend that does not expose a cgroup, or a child that does not join it
  before `exec`.

## Adding a containment claim

Claims live in [THREAT_MODEL.md](THREAT_MODEL.md) and each needs two lines:

```
EVIDENCE: `path/to/implementation.py`, `path/to/test.py::TestClass`
ASSERTS: <gate-suite>::<assertion-name>
```

`security/tm_check.py` fails if a reference does not resolve. Given a gate
result it also fails if the named suite did not execute and pass — a file
that exists is not evidence that anything ran.

Do not add a claim without a test. Mark it `UNVERIFIED` instead; that is an
honest state and the tooling supports it.

## Adding an adversarial fixture

A fixture must probe surfaces specific to its ecosystem rather than repeating
the Python one. It must:

- print a machine-readable report **before** doing anything that could kill
  it — a probe that dies silently proves nothing;
- report non-zero probes, which is how the harness knows the hostile code ran;
- be harmless: canary files and controlled endpoints only, never a real
  exploit.

## Commits

One logical change per commit. The message should say what was wrong and how
it was established, not only what changed — the defect and its evidence are
the durable part.

Do not amend or rewrite the verified release commit
`d05a8b21116639655e26525c62ba2587cab825ad`, and do not force-push.

## Reporting security issues

Not through pull requests — see [SECURITY.md](SECURITY.md).

## Licence

This repository is MIT licensed; see [LICENSE](LICENSE). Contributions are
accepted under the same terms.
