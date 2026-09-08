"""Pure logic: does a table's actual column list satisfy one of our known datasets?

No I/O here - see schema_inspector.py for reading real columns out of a
live database. Kept separate so this comparison (and the malformed-input
edge cases around it) is testable without a database at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from data_console.column_requirements import ALL_DATASETS, DatasetRequirements


@dataclass(frozen=True)
class ColumnCheck:
    name: str
    description: str
    example: str
    present: bool


@dataclass(frozen=True)
class RequirementCheckResult:
    dataset_name: str
    label: str
    satisfied: bool  # every required column is present
    required: list[ColumnCheck] = field(default_factory=list)
    optional: list[ColumnCheck] = field(default_factory=list)


def _normalize(names: list[str]) -> set[str]:
    # Postgres already folds unquoted identifiers to lowercase, so
    # comparing case-insensitively costs nothing for a real Postgres
    # table and adds tolerance for the CSV/spreadsheet sources this
    # console will grow to support next, where a header's exact case
    # isn't something a user should have to get right.
    return {name.strip().lower() for name in names if isinstance(name, str)}


def check_against(available_columns: list[str], requirements: DatasetRequirements) -> RequirementCheckResult:
    """Pure. Compares `available_columns` against one dataset's requirements."""
    available = _normalize(available_columns)

    required_checks = [
        ColumnCheck(col.name, col.description, col.example, present=col.name.lower() in available)
        for col in requirements.required
    ]
    optional_checks = [
        ColumnCheck(col.name, col.description, col.example, present=col.name.lower() in available)
        for col in requirements.optional
    ]

    return RequirementCheckResult(
        dataset_name=requirements.dataset_name,
        label=requirements.label,
        satisfied=all(check.present for check in required_checks),
        required=required_checks,
        optional=optional_checks,
    )


def check_against_all_known_datasets(available_columns: list[str]) -> list[RequirementCheckResult]:
    """Pure. One RequirementCheckResult per known dataset, same fixed order as ALL_DATASETS."""
    return [check_against(available_columns, requirements) for requirements in ALL_DATASETS]
