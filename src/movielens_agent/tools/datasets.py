from ..contracts import ArtifactRef, Contract, DatasetManifest, ToolContext
from ..storage.catalog import DatasetCatalog
from .registry import QueryTool, ToolRegistry


class DescribeInput(Contract):
    dataset_ref: ArtifactRef


def register_dataset_tools(registry: ToolRegistry, catalog: DatasetCatalog) -> None:
    def describe(arguments: DescribeInput, context: ToolContext):
        manifest = catalog.get(arguments.dataset_ref)
        return manifest, [manifest.ref]

    registry.register(QueryTool(
        name="datasets.describe", version="1",
        description="Read the registered schema, file counts and fingerprints of an exact raw dataset version.",
        input_model=DescribeInput, output_model=DatasetManifest, handler=describe,
    ))
