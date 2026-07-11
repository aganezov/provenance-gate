"""claude-science-rollouts — a browser-driven evaluation harness that measures the provenance gate's
thesis with controlled, automated Claude Science rollouts.

Python owns the control plane: scenario compilation, episode/replicate orchestration, conditions
and policy, the read-only Operon SQL oracle, checkpoints, evidence manifests, and scoring. A Node
``@playwright/cli`` subpackage owns the browser boundary, driven over subprocess with versioned,
bounded JSON. The harness's structural ground truth is independent of the gate it evaluates.
"""

__version__ = "0.0.1"
