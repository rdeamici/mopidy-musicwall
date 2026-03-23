import json
import logging
from pydantic import BaseModel
from pydantic.fields import Field
from pydantic import ValidationError, conlist, conint
from typing import List

logger = logging.getLogger(__name__)

# Define your schema
class SerialData(BaseModel):
    mac: conlist(conint(ge=0, le=255), min_length=6, max_length=6)  # exactly 6 ints, 0–255
    command: int = Field(..., ge=0, le=3)  # assuming your VALID_INCOMING_COMMANDS are non-negative ints
    message: str

    @property
    def mac_str(self) -> str:
        """Return MAC as colon-separated string (AA:BB:CC:DD:EE:FF)."""
        return ":".join(f"{b:02X}" for b in self.mac)

    @property
    def mac_bytes(self) -> bytes:
        """Return MAC as raw bytes (b'\\xaa\\xbb...')."""
        return self.mac