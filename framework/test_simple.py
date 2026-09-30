"""Small offline smoke test; retained as a directly executable example."""

from coevo.archive import BugArchive
from coevo.controller import CoEvolutionController
from coevo.executor import DuckDBOptimizerOracle
from coevo.generator import SeedCandidateGenerator


def main() -> int:
    controller = CoEvolutionController(
        generator=SeedCandidateGenerator(),
        oracle=DuckDBOptimizerOracle(repetitions=2),
        archive=BugArchive(),
    )
    result = controller.run(iterations=1, batch_size=5)[0]
    print(
        f"executed={result.valid}, invalid={result.invalid}, "
        f"confirmed_new_bugs={result.confirmed_new_bugs}, new_plans={result.unique_plans}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
