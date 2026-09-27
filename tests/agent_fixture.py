"""Isolated protocol test doubles; never used for real model acceptance."""
from contextlib import redirect_stderr
import io
from pathlib import Path

from movielens_agent.agent.model import CompatibleModel
from movielens_agent.agent.service import AgentService
from movielens_agent.agent.settings import Settings
from movielens_agent.cli import save_profile
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.storage.catalog import DatasetCatalog
from movielens_agent.storage.conversations import ConversationStore
from movielens_agent.storage.tasks import TaskStore


def answer(text="测试回答"):
    return {"message": {"role": "assistant", "content": text}, "usage": {}}


def call(name, arguments, identifier="provider-call"):
    import json
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": identifier, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments) if not isinstance(arguments, str) else arguments}}]}, "usage": {}}


class ScriptedModel(CompatibleModel):
    def __init__(self, settings, responses):
        super().__init__(settings)
        self.responses, self.requests = list(responses), []

    def complete(self, payload):
        self.requests.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(payload) if callable(response) else response


class AgentFixture:
    def __init__(self, root):
        self.root, self.db = root, root / "state.sqlite3"
        source = root / "raw"
        source.mkdir()
        for name, value in {"users": "1::F::18::4::00100\n", "movies": "1::Toy (1995)::Animation\n",
                            "ratings": "1::1::5::978307200\n"}.items():
            (source / (name + ".dat")).write_text(value)
        with redirect_stderr(io.StringIO()):
            self.ref = save_profile(source, root / "profile", self.db)["dataset_ref"]
        self.settings = Settings(catalog=self.db, model_url="https://primary.invalid/v1", model_name="test-model",
                                 model_api_key="test-primary-secret", backup_api_key="test-backup-secret")
        self.chats = ConversationStore(self.db)
        self.chats.initialize()
        self.session = self.chats.create_session()["session_id"]
        self.tasks, self.catalog = TaskStore(self.db), DatasetCatalog(self.db)
        self.config = GovernanceConfig.read(Path("configs/governance/default.json"))

    def agent(self, responses):
        self.model = ScriptedModel(self.settings, responses)
        return AgentService(self.settings, self.chats, self.tasks, self.catalog, self.config, self.model)
