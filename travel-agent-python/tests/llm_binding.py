"""Role-bound capabilities for offline model substitutes."""

from app.common.llm_client import LLMClient
from app.common.model_registry import binding


class FakeBinding:
    def json_response_format(self, name, schema):
        role = binding("fast")
        return LLMClient(
            base_url=role.base_url, api_key=role.provider.api_key, model=role.model, capabilities=role.capabilities()
        ).json_response_format(name, schema)
