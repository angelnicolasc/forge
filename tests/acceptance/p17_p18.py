"""Validate guarded rollout and revision leases against an installed candidate in Vast."""

from p04_p13 import acceptance_main

if __name__ == "__main__":
    raise SystemExit(acceptance_main(("P17", "P18"), "rollout_trials.py", __doc__))
