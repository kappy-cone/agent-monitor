"""The live-client seam and workspace paths: importable keyless, pins intact.

Mock-first rule: no test constructs a live client — importing the module must
be side-effect free so adapters can be monkeypatched at
``agentmon.llm.live.build_live_client`` without a key in the environment.
"""

from __future__ import annotations

from agentmon.eval import workspace
from agentmon.llm import live


class TestLiveSeam:
    def test_importable_without_any_key_and_pins_intact(self) -> None:
        # Import already succeeded keyless (module-level); the pins are data only.
        assert live.PRIMARY_MODEL == "gemini-3.1-flash-lite"
        assert (live.PRIMARY_RPM, live.PRIMARY_RPD) == (10, 500)
        assert live.MIN_INTERVAL_SECONDS == 60.0 / 10 + 0.2
        assert live.PRIMARY_EXTRA_BODY == {"reasoning_effort": "none"}
        assert live.LOCAL_EXTRA_BODY["chat_template_kwargs"] == {"enable_thinking": False}
        assert callable(live.build_live_client)


class TestWorkspace:
    def test_paths_anchor_to_the_repo(self) -> None:
        assert workspace.LABELS_PATH.exists()
        assert workspace.SPLIT_PATH.exists()
        assert workspace.TRANSCRIPTS_DIR.is_dir()
        assert workspace.PROMPTS_DIR.is_dir()
        assert workspace.FREEZE_MANIFEST.exists()
        assert workspace.BANDS_DIR.is_dir()
        assert workspace.CACHE_DIR == workspace.REPO / ".agentmon_cache"
        assert workspace.PHASE4_LEDGER == workspace.OUT_PHASE4 / "requests.jsonl"
