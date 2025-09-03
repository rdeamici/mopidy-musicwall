from pydantic import BaseModel
from pydantic.fields import Field
from pydantic import ValidationError, conlist, conint
from typing import List

# Define your schema
class SerialData(BaseModel):
    mac: conlist(conint(ge=0, le=255), min_length=6, max_length=6)  # exactly 6 ints, 0–255
    command: int = Field(..., ge=0)  # assuming your VALID_INCOMING_COMMANDS are non-negative ints
    message: str


def transform_serial(self, data):
    line = data.decode("utf-8").rstrip().strip()
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        logger.debug(
            f"MusicWallFrontend: JSON DECODE ERROR FOR OBJ: '{line}' - len({len(line)})"
        )
        return None

    try:
        serial_data = SerialData.model_validate(obj)
    except ValidationError as e:
        logger.debug(f"MusicWallFrontend: JSON INVALID: '{line}' - len({len(line)})")
        # Fallback: handle special ack cases
        if obj in ["0", "1"]:
            self.handle_ack(obj)
        return None

    # extra validation: enforce command must be in your VALID_INCOMING_COMMANDS
    if serial_data.command not in VALID_INCOMING_COMMANDS:
        logger.debug(
            f"MusicWallFrontend: INVALID COMMAND: '{serial_data.command}' - OBJ: '{line}'"
        )
        return None

    # process validated data
    mac = ":".join(f"{b:02X}" for b in serial_data.mac)
    message = serial_data.message
    cmd = serial_data.command

    if cmd != DEBUG:
        self.mac_album_dict[mac] = message
        self.mac_album_dict[message] = mac

    self.handle_command(cmd, message)
