"""Atomic benchmark snapshots in the shared application SQLite database."""

import sqlite3
from datetime import datetime

from modelpilot.benchmarks.evaluators import CaseEvaluation
from modelpilot.benchmarks.results import BenchmarkCaseResult, BenchmarkRun, BenchmarkRunConfig
from modelpilot.benchmarks.store import BenchmarkStoreDataError, DuplicateBenchmarkRunError
from modelpilot.sqlite_database import SQLiteDatabase, serialize_datetime

_CONFIG_FIELDS = tuple(BenchmarkRunConfig.model_fields)
_EVALUATION_COLUMNS = {
    "evaluation_score": "score",
    "evaluation_passed": "passed",
    "evaluator_kind": "evaluator_kind",
    "evaluator_version": "evaluator_version",
    "evaluation_reason": "reason",
}


class SQLiteBenchmarkStore(SQLiteDatabase):
    """Pass Settings.metrics_db_path, the same path used by SQLiteMetricsStore."""

    def save_run(self, run: BenchmarkRun) -> None:
        # Revalidate even unchecked model_copy/model_construct input before writing.
        run = BenchmarkRun.model_validate(run.model_dump())
        fields = run.model_dump(exclude={"case_results", "config"})
        fields.update(run.config.model_dump())
        fields["started_at"] = serialize_datetime(run.started_at)
        fields["finished_at"] = serialize_datetime(run.finished_at)
        fields["terminated_early"] = int(run.terminated_early)
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM benchmark_runs WHERE run_id = ?", (run.run_id,)
            ).fetchone():
                raise DuplicateBenchmarkRunError("benchmark run ID already exists")
            _insert(connection, "benchmark_runs", fields)
            for index, case in enumerate(run.case_results):
                values = case.model_dump(exclude={"evaluation"})
                values.update(run_id=run.run_id, case_index=index)
                for column, field in _EVALUATION_COLUMNS.items():
                    value = getattr(case.evaluation, field) if case.evaluation else None
                    values[column] = (
                        int(value)
                        if column == "evaluation_passed" and (value is not None)
                        else value
                    )
                _insert(connection, "benchmark_case_results", values)

    def get_run(self, run_id: str) -> BenchmarkRun | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM benchmark_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                if connection.execute(
                    "SELECT 1 FROM benchmark_case_results WHERE run_id = ? LIMIT 1", (run_id,)
                ).fetchone():
                    raise BenchmarkStoreDataError("orphan benchmark case results")
                return None
            return _restore_run(connection, row)

    def list_runs(self, limit: int = 20) -> list[BenchmarkRun]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self._connect() as connection:
            connection.execute("BEGIN")
            if connection.execute(
                "SELECT 1 FROM benchmark_case_results AS c WHERE NOT EXISTS "
                "(SELECT 1 FROM benchmark_runs AS r WHERE r.run_id = c.run_id) LIMIT 1"
            ).fetchone():
                raise BenchmarkStoreDataError("orphan benchmark case results")
            rows = connection.execute(
                "SELECT * FROM benchmark_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_restore_run(connection, row) for row in rows]

    def list_case_results(self, run_id: str) -> tuple[BenchmarkCaseResult, ...]:
        run = self.get_run(run_id)
        return run.case_results if run is not None else ()


def _insert(connection: sqlite3.Connection, table: str, fields: dict[str, object]) -> None:
    # Identifiers originate only from fixed domain field names, never user strings.
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )


def _boolean(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("invalid persisted boolean")
    return bool(value)


def _restore_run(connection: sqlite3.Connection, row: sqlite3.Row) -> BenchmarkRun:
    try:
        fields = dict(row)
        fields["started_at"] = datetime.fromisoformat(fields["started_at"])
        fields["finished_at"] = datetime.fromisoformat(fields["finished_at"])
        fields["terminated_early"] = _boolean(fields["terminated_early"])
        fields["config"] = {key: fields.pop(key) for key in _CONFIG_FIELDS}
        rows = connection.execute(
            "SELECT * FROM benchmark_case_results WHERE run_id = ? ORDER BY case_index ASC",
            (row["run_id"],),
        ).fetchall()
        if [r["case_index"] for r in rows] != list(range(len(rows))):
            raise ValueError("non-contiguous case indices")
        fields["case_results"] = tuple(_restore_case(case) for case in rows)
        return BenchmarkRun.model_validate(fields)
    except (ValueError, TypeError, KeyError) as error:
        raise BenchmarkStoreDataError("malformed benchmark run") from error


def _restore_case(row: sqlite3.Row) -> BenchmarkCaseResult:
    fields = dict(row)
    fields.pop("run_id")
    fields.pop("case_index")
    evaluation = {field: fields.pop(column) for column, field in _EVALUATION_COLUMNS.items()}
    if all(value is None for value in evaluation.values()):
        fields["evaluation"] = None
    else:
        evaluation["passed"] = _boolean(evaluation["passed"])
        fields["evaluation"] = CaseEvaluation.model_validate(evaluation)
    return BenchmarkCaseResult.model_validate(fields)
