"""Run the new-task suite using the same objective scorer and explicit CLI limits."""
from eval import run_objective
from eval.holdout_cases import CASES, SUITE_VERSION


def main() -> None:
    """Select the separately versioned tasks; preserve original six-task definitions."""
    run_objective.CASES = CASES
    run_objective.SUITE_VERSION = SUITE_VERSION
    run_objective.main()


if __name__ == "__main__":
    main()
