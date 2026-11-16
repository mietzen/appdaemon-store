"""Atomic JSON state persistence for AppDaemon Store."""

import logging
import os
from pathlib import Path
import tempfile

from pydantic import ValidationError as PydanticValidationError

from appdaemon_store.exceptions import StoreError
from appdaemon_store.type_definitions import StoreState

logger = logging.getLogger(__name__)


class StateStore:
    """Persist and load StoreState atomically as JSON."""

    def __init__(self, state_file: Path) -> None:
        """Initialize the store with the target state JSON path."""
        self._state_file = state_file

    def load(self) -> StoreState:
        """Load StoreState from disk, returning empty state if file is absent."""
        if not self._state_file.is_file():
            return StoreState()

        try:
            raw = self._state_file.read_text(encoding="utf-8")
        except OSError as err:
            raise StoreError(
                f"Failed reading state file {self._state_file}: {err}"
            ) from err

        if not raw.strip():
            return StoreState()

        try:
            return StoreState.model_validate_json(raw)
        except PydanticValidationError as err:
            raise StoreError(
                f"Invalid state JSON in {self._state_file}: {err}"
            ) from err

    def save(self, state: StoreState) -> None:
        """Atomically persist StoreState to disk using temp file and os.replace."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2) + "\n"
        tmp_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._state_file.parent,
                prefix=f".{self._state_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._state_file)
        except OSError as err:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise StoreError(
                f"Failed saving state file {self._state_file}: {err}"
            ) from err
