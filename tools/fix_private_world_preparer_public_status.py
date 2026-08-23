from __future__ import annotations

from pathlib import Path


TARGET = Path(__file__).with_name(
    "prepare_original_private_world_direct_management.py"
)


STATUS_PATCH = '''    value = replace_once(
        value,
        '            "private_world_mounted": self.private_world_read is not None,\\n            "candidate_store_mounted": self.candidate_store is not None,\\n',
        '            "private_world_mounted": self.private_world_read is not None,\\n            "private_world_commands_mounted": (\\n                self.private_world_commands is not None\\n            ),\\n            "candidate_store_mounted": self.candidate_store is not None,\\n',
        "SERVER_STATUS",
    )
'''

TEST_ASSERTION = '''        assert runtime.private_world_commands is not None
        assert runtime.public_status()["private_world_commands_mounted"] is True
'''

TEST_REPLACEMENT = '''        assert runtime.private_world_commands is not None
'''


def main() -> None:
    value = TARGET.read_text(encoding="utf-8")
    if value.count(STATUS_PATCH) != 1:
        raise RuntimeError("PRIVATE_WORLD_PUBLIC_STATUS_PATCH_ANCHOR_INVALID")
    if value.count(TEST_ASSERTION) != 1:
        raise RuntimeError("PRIVATE_WORLD_PUBLIC_STATUS_TEST_ANCHOR_INVALID")
    value = value.replace(STATUS_PATCH, "", 1)
    value = value.replace(TEST_ASSERTION, TEST_REPLACEMENT, 1)
    TARGET.write_text(value, encoding="utf-8")
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
