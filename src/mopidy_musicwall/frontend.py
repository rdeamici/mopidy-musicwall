import logging
import os
import threading
import time
import json
import pykka
import requests
import io
import serial
from mopidy.core import CoreListener
from urllib.parse import quote
from enum import Enum, auto

from . import Extension

logger = logging.getLogger(__name__)

PLAY = 0
STOP = 1
REGISTER = 2
DEBUG = 3

VALID_INCOMING_COMMANDS = { PLAY, STOP, REGISTER, DEBUG }

REGISTER_ACK = 0
INFO = 1
LIGHT_ON = 2
LIGHT_OFF = 3
POWER_ON = 4
POWER_OFF = 5
NEW_CENTRAL = 6

VALID_OUTGOING_COMMANDS = { REGISTER_ACK, INFO, LIGHT_ON, LIGHT_OFF, POWER_ON, POWER_OFF, NEW_CENTRAL }


OUTGOING_SERIAL_COMMANDS = {
    "INFO": 1,
    "LIGHT_ON": 2,
    "LIGHT_OFF": 3,
    "POWER_ON": 4,
    "POWER_OFF": 5,
}

class OutgoingSerialHandler(pykka.ThreadingActor):
    def __init__(self, serial_port):
        super(OutgoingSerialHandler, self).__init__()
        self.serial_port = serial_port
        self.lock = threading.Lock()
        logger.debug("OutgoingSerialHandler initialized")
        logger.debug(f"OutgoingSerialHandler serial_port is open? {self.serial_port.is_open}")

    def send_message(self, message: str):
        with self.lock:
            logger.debug(f"OutgoingSerialHandler sending message: {message}")
            try:
                numBytes = self.serial_port.write((message + '\n').encode('utf-8'))   # newline is important
                logger.debug(f"OutgoingSerialHandler sent {numBytes} bytes")
            except Exception as e:
                logger.debug(f"OutgoingSerialHandler encountered an error: {e}")
class IncomingSerialHandler(pykka.ThreadingActor):
    def __init__(self, serial_port, frontend_proxy):
        super(IncomingSerialHandler, self).__init__()
        self.frontend_proxy = frontend_proxy
        self.serial_port = serial_port
        logger.debug("IncomingSerialHandler initialized")
        logger.debug(f"IncomingSerialHandler serial_port is open? {self.serial_port.is_open}")

    def on_start(self):
        self.running = True
        self.thread = threading.Thread(target=self.read_loop, daemon=True)
        self.thread.start()    
    
    def read_loop(self):
        while self.running:
            line = self.serial_port.readline()
            if line:
                logger.debug(f"IncomingSerialHandler received: {line}")
                self.frontend_proxy.transform_serial(line)
            
    def on_stop(self):
        self.running = False
        if self.thread.is_alive():
            self.thread.join()
            


class MusicWallFrontend(pykka.ThreadingActor, CoreListener):
    def __init__(self, config, core):
        super(MusicWallFrontend, self).__init__()
        self.core = core
        self.config = config["musicwall"]

        # relies on mopidy-http to be configured and running
        self.ser_port = self.config.get("port")
        self.baudrate = self.config.get("baudrate")
        self.mac_album_dict = {}
        self.current_album = None
        self.state = "listening"
        logger.debug(f"MusicWallFrontend initialized on serial port {self.ser_port} with baudrate {self.baudrate}")

    def on_start(self):
        self.serial = serial.Serial(self.ser_port, self.baudrate, timeout=1)
        self.incoming_handler = IncomingSerialHandler.start(self.serial, self.actor_ref.proxy())
        self.outgoing_handler_proxy = OutgoingSerialHandler.start(self.serial).proxy()

    def on_stop(self):
        self.serial.close()
        self.incoming_handler.stop()
        self.outgoing_handler_proxy.actor_ref.stop()

    def tracklist_changed(self):
        """Called by Mopidy when the tracklist changes."""
        # ignore for now: experiment
        return
        logger.debug(f"MusicWallFrontend: tracklick_changed detected - current_album: {self.current_album}")
        logger.debug(f"MusicWallFrontend: tracklist_length: {self.core.tracklist.get_length().get()}")
        # current album has been stopped
        if self.current_album and self.core.tracklist.get_length().get() == 0:
            logger.debug("MusicWallFrontend: tracklist is empty!")
            peripheral_mac = self.mac_album_dict[self.current_album]
            self.current_album = None
            if peripheral_mac:
                success = self.send_to_esp(peripheral_mac, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])
                cmd = OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"]
                if success:
                    logger.debug(f"MusicWallFrontend: send to esp succeeded with params mac {peripheral_mac} command {cmd}")
                    self.state = "ack_expected"
                else:
                    logger.warning(f"MusicWallFrontend: send to esp failed for mac {peripheral_mac} command {cmd}")
            else:
                logger.debug(f"MusicWallFrontend ERROR: album ({current_album}) is not associated with any known peripheral mac addresses")
                logger.debug(json.dumps(self.mac_album_dict, indent=2))

        
        # new album added
        elif not self.current_album and self.core.tracklist.get_length().get() > 0:
            logger.debug("MusicWallFrontend: tracklist contains songs!")
            current_album = self.get_current_album()
            peripheral_mac = self.mac_album_dict[current_album]
            if peripheral_mac:
                self.current_album = current_album
                self.core.playback.play().get()
                self.send_to_esp(peripheral_mac, OUTGOING_SERIAL_COMMANDS["LIGHT_ON"])
                cmd = OUTGOING_SERIAL_COMMANDS["LIGHT_ON"]
            else:
                logger.debug(f"MusicWallFrontend ERROR: album ({current_album}) is not associated with any known peripheral mac addresses")
                logger.debug(json.dumps(self.mac_album_dict, indent=2))
        else:
            logger.debug(f"MusicWallFrontend: something wrong! self.current_album: {self.current_album} - tracklist.get_length() {self.core.tracklist.get_length().get()}")

    def handle_ack(self, ack: str):
        """
        Handle acknowledgement messages from the ESP.
        Expects '1' for success, anything else is considered a failure.
        """
        if ack == "1":
            logger.debug(f"MusicWallFrontend: ACK received")
            success = True
        else:
            logger.debug(f"MusicWallFrontend: ACK failed or unexpected response: '{ack}'")
            success = False

        # reset state
        self.state = "listening"

        return {"ack": success}

    def get_album_uri(self, album_name: str, media_dir="/media/usb/music"):
        # Replace spaces with underscores to match your filesystem
        folder_name = album_name.replace(" ", "_")
        path = f"{media_dir}/{folder_name}/"
        # Encode special characters (spaces, etc.) for a valid URI
        return f"file://{quote(path)}"

    def get_album_track_uris(self, album_uri: str):
        refs = self.core.library.browse(album_uri).get()
        track_uris = [ref.uri for ref in refs if ref.type == "track"]
        return track_uris

    def handle_command(self, cmd, new_message):
        if cmd == REGISTER:
            logger.debug(f"MusicWallFrontend: handle_command called with REGISTER: '{cmd}' '{new_message}'")
            register_mac = self.mac_album_dict[new_message]
            self.send_to_esp(register_mac, REGISTER_ACK)
            return
        elif cmd == DEBUG:
            logger.debug(f"MusicWallFrontend - DEBUG message from TRANSCEIVER: {new_message}")
            return
        elif cmd == PLAY:
            logger.debug(f"MusicWallFrontend - handling PLAY")
            self.handle_play(new_message)
        elif cmd == STOP:
            logger.debug(f"MusicWallFrontend - handling STOP")
            self.handle_stop()
        else:
            logger.debug(f"MusicWallFrontend - UNKNOWN COMMAND: {cmd}")


    def handle_stop(self):
        playing = self.core.playback.get_state().get() == "playing"
        logger.debug(f"MusicWallFrontend - handling stop - playing? {playing}")
        if playing:
            self.core.playback.stop().get()
            self.core.tracklist.clear().get()

    def handle_play(self, new_album):
        current_album = self.get_current_album()
        logger.debug(f"MusicWallFrontend - handle_play - current_album: {current_album} - new_album: {new_album}")
        if new_album == current_album:
            return
        
        self.handle_stop()
        logger.debug("MusicWallFrontend: playing new_album")
        album_uri = self.get_album_uri(new_album)
        track_uris = self.get_album_track_uris(album_uri)
        logger.debug(f"MusicWallFrontend: playing track_uris {track_uris}")
        self.core.tracklist.add(uris=track_uris).get()
        self.core.playback.play().get()


    def send_to_esp(self, mac_address, command):
        mac = [int(b, 16) for b in mac_address.split(":")]
        payload = {
            "mac": mac,
            "command": command
        }
        json_str = json.dumps(payload)

        # Send over serial
        self.outgoing_handler_proxy.send_message(json_str)

    def get_current_album(self):
        logger.debug(f"MusicWallFrontend - getting current album")
        numTracks = self.core.tracklist.get_length().get()
        if numTracks == 0:
            logger.debug(f"MusicWallFrontend - get_current_album = nothing in tracklist")
            return None

        tltrack = self.core.tracklist.slice(0,1).get()[0]
        track = tltrack.track
        if track and track.uri:
            path = track.uri.replace("file://", "")
            folder = os.path.basename(os.path.dirname(path))
            logger.debug(f"MusicWallFrontend returning album name: '{folder}' from uri: {track.uri} - path: {path}")
            return tltrack.track.album.name

        logger.debug(f"MusicWallFrontend - missing track or track.uri - tltrack: {tltrack}")
        return None

    def transform_serial(self, data):
        line = data.decode('utf-8').rstrip().strip()
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"MusicWallFrontend: JSON DECODE ERROR FOR OBJ: '{line}' - len({len(line)})")
            return None

        valid = (
            isinstance(data, dict)
            and "mac" in data
            and "command" in data
            and "message" in data
            and isinstance(data["mac"], list)
            and len(data["mac"]) == 6
            and all(isinstance(b, int) and 0 <= b <= 255 for b in data["mac"])
            and isinstance(data["command"], int)
            and data["command"] in VALID_INCOMING_COMMANDS
            and isinstance(data["message"], str)
        )

        if valid:
            logger.debug(f"MusicWallFrontend validated json: '{json.dumps(data)}'")
            mac = ":".join(f'{b:02X}' for b in data["mac"])
            message = data["message"]
            cmd = data["command"]
            if cmd != DEBUG:
                self.mac_album_dict[mac] = message
                self.mac_album_dict[message] = mac
            logger.debug(f"MusicWallFrontend: calling handle_command with {cmd}, {message}")
            self.handle_command(cmd, message)
        else:
            logger.debug(f"MusicWallFrontend: JSON INVALID: '{line}' - len({len(line)})")
            if data in ["0", "1"]:
                self.handle_ack(data)
