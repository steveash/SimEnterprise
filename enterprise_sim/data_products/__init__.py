"""Structured data products: causal-graph-driven synthetic tables (docs/DATA_PRODUCTS.md).

This subsystem extends Enterprise Sim beyond unstructured documents: a **data
scenario** (product growth, sales pipeline, support ops, …) declares a causal
graph over categorical and numerical variables, samples realistic tabular data
from it at scale into partitioned parquet, materializes SQL views over those
tables, and then runs an iterative *question loop* — propose business
questions, evaluate whether the data can answer them, and patch the spec to
fill the recognized gaps.

Mirrors the document pipeline's contracts: a deterministic seeded skeleton
(scenario template plugins) that an LLM may author or extend through the
``core.llm`` client, so everything runs keyless on the ``fake`` backend and
gets richer on real backends.
"""

from enterprise_sim.data_products.config import DataRunConfig, load_data_config
from enterprise_sim.data_products.runner import DataRunResult, execute_data_run
from enterprise_sim.data_products.spec import ScenarioSpec

__all__ = [
    "DataRunConfig",
    "DataRunResult",
    "ScenarioSpec",
    "execute_data_run",
    "load_data_config",
]
