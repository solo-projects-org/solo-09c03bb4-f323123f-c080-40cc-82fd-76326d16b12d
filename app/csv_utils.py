"""CSV parsing for back-filled counter exports."""

from __future__ import annotations

import csv
import io

from .timeline import Record, parse_local

REQUIRED_COLUMNS = {"sample_id", "local_time", "pass_count"}


class CsvValidationError(ValueError):
    pass


def parse_counter_csv(content: bytes) -> list[Record]:
    """Validate and parse an uploaded CSV.

    Header must contain exactly the columns:
    ``sample_id,local_time,pass_count`` (extra columns are rejected so a
    misaligned export cannot silently shift data).
    """

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvValidationError(f"CSV must be UTF-8 encoded: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvValidationError("empty CSV or missing header row")

    header = {name.strip() for name in reader.fieldnames}
    missing = REQUIRED_COLUMNS - header
    if missing:
        raise CsvValidationError(
            f"missing required column(s): {', '.join(sorted(missing))}; "
            f"expected sample_id,local_time,pass_count"
        )
    extra = header - REQUIRED_COLUMNS
    if extra:
        raise CsvValidationError(
            f"unexpected column(s): {', '.join(sorted(extra))}; "
            f"expected sample_id,local_time,pass_count"
        )

    records: list[Record] = []
    seen: set[tuple[int, str, int]] = set()
    for line_no, row in enumerate(reader, start=2):
        if row is None or all((v is None or v.strip() == "") for v in row.values()):
            continue  # tolerate trailing blank lines

        raw_id = (row.get("sample_id") or "").strip()
        raw_local = (row.get("local_time") or "").strip()
        raw_count = (row.get("pass_count") or "").strip()
        if not raw_id or not raw_local or not raw_count:
            raise CsvValidationError(f"line {line_no}: all three columns are required")

        try:
            sample_id = int(raw_id)
        except ValueError as exc:
            raise CsvValidationError(
                f"line {line_no}: sample_id must be an integer, got {raw_id!r}"
            ) from exc
        if sample_id <= 0:
            raise CsvValidationError(
                f"line {line_no}: sample_id must be a positive integer"
            )

        try:
            parse_local(raw_local)
        except ValueError as exc:
            raise CsvValidationError(f"line {line_no}: {exc}") from exc

        try:
            pass_count = int(raw_count)
        except ValueError as exc:
            raise CsvValidationError(
                f"line {line_no}: pass_count must be an integer, got {raw_count!r}"
            ) from exc
        if pass_count < 0:
            raise CsvValidationError(
                f"line {line_no}: pass_count must be non-negative"
            )

        record = Record(sample_id, raw_local, pass_count)
        if record.key() in seen:
            raise CsvValidationError(
                f"line {line_no}: duplicate row for sample_id {sample_id} "
                f"inside the same CSV"
            )
        seen.add(record.key())
        records.append(record)

    if not records:
        raise CsvValidationError("CSV contains a header but no data rows")
    return records
