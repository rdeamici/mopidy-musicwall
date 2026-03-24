import json
import os
import logging
from pydantic import BaseModel
from pydantic.fields import Field
from pydantic import ValidationError, conlist, conint
from typing import List

logger = logging.getLogger(__name__)

# Define your schema
class SerialData(BaseModel):
    mac: conlist(conint(ge=0, le=255), min_length=6, max_length=6)  # exactly 6 ints, 0–255
    command: int = Field(..., ge=0, le=4)  # assuming your VALID_INCOMING_COMMANDS are non-negative ints
    message: str

    @property
    def mac_str(self) -> str:
        """Return MAC as colon-separated string (AA:BB:CC:DD:EE:FF)."""
        return ":".join(f"{b:02X}" for b in self.mac)

class Frame:
    def __init__(self, mac, album_uri=""):
        # persistent fields saved to json
        self.mac = mac
        self.album_uri = album_uri
        
        # volatile fields that are reset on reboot
        self.is_lit = False

    def to_dict(self):
        return {
            "mac": self.mac,
            "album_uri": self.album_uri,
        }



class FrameRegistry:
    def __init__(self, db_path="music_wall_db.json"):
        self._db_path = db_path
        self._frames = self._load_from_disk()
    

    def _load_from_disk(self):
        if os.path.exists(self._db_path):
            try:
                with open(self._db_path, "r") as f:
                    data = json.load(f)
                    return { mac: Frame(**val) for mac, val in data.items() }
            except Exception as e:
                logger.error(f"Registry: Failed to load JSON from file: {e}")

        return {}


    def save_to_disk(self):
        try:
            data = { mac: f.to_dict() for mac, f in self._frames.items() }
            temp_path = f"{self._db_path}.tmp"
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=4)
            os.replace(temp_path, self._db_path)
        except Exception as e:
            logger.error(f"Registry: Failed to save JSON to file: {e}")


    def add_or_update(self, mac, uri=""):
        if mac not in self._frames:
            self._frames[mac] = Frame(mac)    
        if uri:
            self._frames[mac].album_uri = uri
        self.save_to_disk()

    def set_lit_state(self, mac, is_lit = True):
        self._frames[mac].is_lit = is_lit

    # --- Lookups ---

    def by_mac(self, mac):
        """Quick O(1) lookup."""
        return self._frames.get(mac)


    def by_album_uri(self, album_uri):
        """Search for a frame by its URI."""
        for frame in self._frames.values():
            if frame.album_uri == album_uri:
                return frame
        return None


    def get_lit_frames(self):
        """Get a list of all frames that currently have their light on."""
        return [f for f in self._frames.values() if f.is_lit]


    def all_frames(self):
        return self._frames.values()


    def __len__(self):
        """Returns the number of registered frames."""
        return len(self._frames)