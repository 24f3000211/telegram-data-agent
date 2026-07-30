"""Dataset discovery, loading, and safe deterministic analyses."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import requests

from utils import csv_text_to_buffer, url_filename

SUPPORTED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".zip"}


def download_file(url: str, destination_dir: str, max_bytes: int = 25_000_000) -> Path:
    """Download a public file with size checks and return its local path."""
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / url_filename(url)
    try:
        with requests.get(url, stream=True, timeout=(10, 45), headers={"User-Agent": "telegram-data-agent/1.0"}) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length", "0"))
            if content_length > max_bytes:
                raise ValueError("Dataset exceeds configured download size limit")
            total = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Dataset exceeds configured download size limit")
                    handle.write(chunk)
    except requests.RequestException as exc:
        target.unlink(missing_ok=True)
        raise ValueError(f"Could not download dataset: {exc}") from exc
    return target


def detect_dataset(source: str | Path) -> str:
    """Return normalized dataset type based on a file name or URL."""
    suffix = Path(str(source).split("?", 1)[0]).suffix.lower()
    return {
        ".csv": "csv", ".tsv": "tsv", ".txt": "csv", ".xlsx": "excel",
        ".xls": "excel", ".json": "json", ".zip": "zip",
    }.get(suffix, "unknown")


def load_csv(source: str | Path | io.StringIO, separator: str | None = None) -> pd.DataFrame:
    """Load CSV/TSV while allowing pandas to infer uncommon delimiters."""
    if separator:
        return pd.read_csv(source, sep=separator)
    return pd.read_csv(source, sep=None, engine="python")


def load_excel(source: str | Path) -> pd.DataFrame:
    """Load the first worksheet from an Excel workbook."""
    return pd.read_excel(source)


def load_json(source: str | Path | io.StringIO) -> pd.DataFrame:
    """Load record-oriented or common JSON datasets."""
    return pd.read_json(source)


def load_inline_csv(content: str) -> pd.DataFrame:
    """Load user-provided delimited text."""
    return load_csv(csv_text_to_buffer(content))


def _load_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        candidates = [name for name in archive.namelist() if detect_dataset(name) in {"csv", "tsv", "json"}]
        if not candidates:
            raise ValueError("ZIP does not contain a CSV, TSV, or JSON dataset")
        name = candidates[0]
        with archive.open(name) as member:
            data = member.read()
        if detect_dataset(name) == "json":
            return load_json(io.StringIO(data.decode("utf-8-sig")))
        return load_csv(io.StringIO(data.decode("utf-8-sig")))


def load_dataframe(source: str | Path, inline: bool = False) -> pd.DataFrame:
    """Load a supported local dataset or inline body into a dataframe."""
    if inline:
        kind = "json" if str(source).lstrip().startswith(("{", "[")) else "csv"
    else:
        kind = detect_dataset(source)
    if kind in {"csv", "tsv"}:
        return load_inline_csv(str(source)) if inline else load_csv(source, "\t" if kind == "tsv" else None)
    if kind == "excel":
        return load_excel(source)
    if kind == "json":
        return load_json(io.StringIO(str(source)) if inline else source)
    if kind == "zip":
        return _load_zip(Path(source))
    raise ValueError("Unsupported dataset type; use CSV, TSV, Excel, JSON, or ZIP containing CSV/JSON")


def dataframe_profile(frame: pd.DataFrame) -> dict[str, Any]:
    """Create a compact, JSON-safe dataframe profile for the model and logs."""
    return {
        "rows": int(len(frame)),
        "columns": [{"name": str(name), "dtype": str(dtype)} for name, dtype in frame.dtypes.items()],
        "missing_values": {str(k): int(v) for k, v in frame.isna().sum().items()},
        "sample": frame.head(10).where(pd.notna(frame.head(10)), None).to_dict(orient="records"),
    }


def run_pandas_analysis(frame: pd.DataFrame, question: str) -> dict[str, Any]:
    """Return useful deterministic statistics; never executes user code."""
    numeric = frame.select_dtypes(include="number")
    result: dict[str, Any] = {"profile": dataframe_profile(frame)}
    if not numeric.empty:
        result["numeric_summary"] = numeric.describe().round(6).to_dict()
        if numeric.shape[1] > 1 and re.search(r"correlat", question, re.I):
            result["correlation"] = numeric.corr().round(6).to_dict()
        if numeric.shape[1] >= 2 and re.search(r"regression|linear model|predict", question, re.I):
            x_name, y_name = numeric.columns[:2]
            clean = numeric[[x_name, y_name]].dropna()
            if len(clean) >= 2 and clean[x_name].nunique() > 1:
                slope, intercept = np.polyfit(clean[x_name], clean[y_name], 1)
                predicted = slope * clean[x_name] + intercept
                residual_sum = float(((clean[y_name] - predicted) ** 2).sum())
                total_sum = float(((clean[y_name] - clean[y_name].mean()) ** 2).sum())
                result["linear_regression"] = {
                    "x": str(x_name), "y": str(y_name), "slope": round(float(slope), 6),
                    "intercept": round(float(intercept), 6),
                    "r_squared": round(1 - residual_sum / total_sum, 6) if total_sum else None,
                    "observations": int(len(clean)),
                }
    if re.search(r"revenue|sales", question, re.I):
        normalized_columns = {str(column).strip().lower(): column for column in frame.columns}
        product_column = normalized_columns.get("product")
        price_column = normalized_columns.get("price")
        quantity_column = normalized_columns.get("quantity")
        if product_column is not None and price_column is not None and quantity_column is not None:
            revenue_frame = frame[[product_column, price_column, quantity_column]].copy()
            revenue_frame["_revenue"] = (
                pd.to_numeric(revenue_frame[price_column], errors="coerce")
                * pd.to_numeric(revenue_frame[quantity_column], errors="coerce")
            )
            grouped_revenue = revenue_frame.groupby(product_column, dropna=False)["_revenue"].sum(min_count=1).dropna()
            if not grouped_revenue.empty:
                revenue_by_product = {
                    str(product): float(revenue)
                    for product, revenue in grouped_revenue.sort_values(ascending=False).items()
                }
                highest_product = grouped_revenue.idxmax()
                result["revenue"] = {
                    "by_product": revenue_by_product,
                    "highest_product": str(highest_product),
                    "highest_revenue": float(grouped_revenue.loc[highest_product]),
                    "overall_revenue": float(grouped_revenue.sum()),
                }
    mentioned = [column for column in frame.columns if re.search(re.escape(str(column)), question, re.I)]
    if mentioned:
        column = mentioned[0]
        if re.search(r"top|highest|largest", question, re.I):
            result["top_rows"] = frame.sort_values(column, ascending=False).head(10).to_dict(orient="records")
        elif re.search(r"bottom|lowest|smallest", question, re.I):
            result["bottom_rows"] = frame.sort_values(column, ascending=True).head(10).to_dict(orient="records")
        elif re.search(r"value.?count|frequen|mode", question, re.I):
            result["value_counts"] = frame[column].value_counts(dropna=False).head(20).to_dict()
    return result


def run_duckdb_analysis(frame: pd.DataFrame, sql: str) -> pd.DataFrame:
    """Run a read-only SELECT/WITH query against dataframe named ``data``."""
    normalized = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", normalized, re.I):
        raise ValueError("Only read-only SELECT or WITH SQL is allowed")
    connection = duckdb.connect(database=":memory:")
    try:
        connection.register("data", frame)
        return connection.execute(normalized).fetchdf()
    finally:
        connection.close()
