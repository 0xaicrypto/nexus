"""
Regression tests for BNBChainArtifactService.

Covers:
  - save + load round-trip (text, bytes, dict)
  - Versioning: multiple saves increment version
  - Load specific version vs latest
  - list_artifact_keys
  - list_versions / list_artifact_versions
  - delete_artifact removes from manifest
  - get_artifact_version metadata
  - Session-scoped vs global artifacts
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus_core.state import StateManager
from nexus_core.artifact import BNBChainArtifactService


@pytest.fixture
def artifact_svc(state_manager):
    """ArtifactService backed by a clean StateManager."""
    # Register an agent so manifest storage works
    state_manager.register_agent("test-app", "owner-1")
    return BNBChainArtifactService(state_manager)


class TestArtifactSaveLoad:

    def test_save_and_load_text(self, artifact_svc):
        async def _test():
            version = await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="report.txt", artifact="Hello world",
            )
            assert version == 1

            part = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="report.txt",
            )
            assert part is not None
            assert part.text == "Hello world"

        asyncio.run(_test())

    def test_save_and_load_bytes(self, artifact_svc):
        async def _test():
            data = b"\x89PNG\r\n\x1a\n"  # fake PNG header
            version = await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="image.png", artifact=data,
            )
            assert version == 1

            part = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="image.png",
            )
            assert part is not None
            # Bytes artifacts come back as inline_data
            assert part.inline_data.data == data

        asyncio.run(_test())

    def test_save_and_load_dict(self, artifact_svc):
        async def _test():
            obj = {"analysis": {"risk_score": 0.85, "flags": ["MEV"]}}
            version = await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="result.json", artifact=obj,
            )
            assert version == 1

            part = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="result.json",
            )
            assert part is not None
            # Dict artifacts come back as Part — extract text from whichever form
            import json
            raw = part.text if part.text else part.inline_data.data.decode("utf-8")
            loaded = json.loads(raw)
            assert loaded["analysis"]["risk_score"] == 0.85

        asyncio.run(_test())


class TestArtifactVersioning:

    def test_multiple_versions(self, artifact_svc):
        async def _test():
            v1 = await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="report.txt", artifact="Version 1",
            )
            v2 = await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="report.txt", artifact="Version 2",
            )
            assert v1 == 1
            assert v2 == 2

            # Load latest (should be v2)
            part = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="report.txt",
            )
            assert part.text == "Version 2"

            # Load specific version
            part_v1 = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="report.txt", version=1,
            )
            assert part_v1.text == "Version 1"

        asyncio.run(_test())

    def test_list_versions(self, artifact_svc):
        async def _test():
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="data.csv", artifact="a,b,c",
            )
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="data.csv", artifact="a,b,c,d",
            )

            versions = await artifact_svc.list_versions(
                app_name="test-app", user_id="user-1",
                filename="data.csv",
            )
            assert versions == [1, 2]

        asyncio.run(_test())

    def test_list_artifact_versions_metadata(self, artifact_svc):
        async def _test():
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="model.bin", artifact=b"\x00\x01\x02",
                custom_metadata={"accuracy": "0.95"},
            )

            av_list = await artifact_svc.list_artifact_versions(
                app_name="test-app", user_id="user-1",
                filename="model.bin",
            )
            assert len(av_list) == 1
            assert av_list[0].version == 1
            assert av_list[0].custom_metadata["accuracy"] == "0.95"
            assert "rune://" in av_list[0].canonical_uri

        asyncio.run(_test())


class TestArtifactManagement:

    def test_list_artifact_keys(self, artifact_svc):
        async def _test():
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="file_a.txt", artifact="a",
            )
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="file_b.txt", artifact="b",
            )

            keys = await artifact_svc.list_artifact_keys(
                app_name="test-app", user_id="user-1",
            )
            assert set(keys) == {"file_a.txt", "file_b.txt"}

        asyncio.run(_test())

    def test_delete_artifact(self, artifact_svc):
        async def _test():
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="temp.txt", artifact="temporary",
            )
            await artifact_svc.delete_artifact(
                app_name="test-app", user_id="user-1",
                filename="temp.txt",
            )

            part = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="temp.txt",
            )
            assert part is None

            keys = await artifact_svc.list_artifact_keys(
                app_name="test-app", user_id="user-1",
            )
            assert "temp.txt" not in keys

        asyncio.run(_test())

    def test_load_nonexistent(self, artifact_svc):
        async def _test():
            part = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="does_not_exist.txt",
            )
            assert part is None

        asyncio.run(_test())

    def test_session_scoped_isolation(self, artifact_svc):
        """Artifacts in different sessions should be isolated."""
        async def _test():
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="log.txt", artifact="session A",
                session_id="sess-a",
            )
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="log.txt", artifact="session B",
                session_id="sess-b",
            )

            part_a = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="log.txt", session_id="sess-a",
            )
            part_b = await artifact_svc.load_artifact(
                app_name="test-app", user_id="user-1",
                filename="log.txt", session_id="sess-b",
            )
            assert part_a.text == "session A"
            assert part_b.text == "session B"

        asyncio.run(_test())

    def test_get_artifact_version(self, artifact_svc):
        async def _test():
            await artifact_svc.save_artifact(
                app_name="test-app", user_id="user-1",
                filename="model.txt", artifact="v1 content",
            )
            av = await artifact_svc.get_artifact_version(
                app_name="test-app", user_id="user-1",
                filename="model.txt",
            )
            assert av is not None
            assert av.version == 1
            assert av.create_time > 0

        asyncio.run(_test())
