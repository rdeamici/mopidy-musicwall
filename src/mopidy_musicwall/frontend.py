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

VALID_INCOMING_COMMANDS = {
    PLAY: "play",
    STOP: "stop",
    REGISTER: "register",
    DEBUG: "debug"
}

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
        # logger.debug(f"OutgoingSerialHandler serial_port is open? {self.serial_port.is_open}")


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
        # logger.debug(f"IncomingSerialHandler serial_port is open? {self.serial_port.is_open}")


    def on_start(self):
        self.running = True
        self.thread = threading.Thread(target=self.read_loop, daemon=True)
        self.thread.start()    


    def read_loop(self):
        while self.running:
            line = self.serial_port.readline()
            if line:
                # logger.debug(f"IncomingSerialHandler received: {line}")
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
        self.core.tracklist.set_consume(True)


    def on_stop(self):
        self.serial.close()
        self.incoming_handler.stop()
        self.outgoing_handler_proxy.actor_ref.stop()


    def on_event(self, event, **kwargs):
        logger.debug(f"MusicWallFrontend: on_event called with event: {event}, kwargs: {kwargs}")
        
        if event == "track_playback_ended":
            self.handle_track_playback_ended(kwargs.get("tl_track"))    
    

    def handle_track_playback_ended(self, tl_track):
        if self.current_album is None:
            logger.debug("MusicWallFrontend: track_playback_ended - current_album is None, stop should have already been handled manually")
        
        elif self.current_album != tl_track.track.album:
            logger.debug(f"MusicWallFrontend: track_playback_ended - old album `{tl_track.track.album}` ended, current album `{self.current_album}` should be playing")

        elif self.core.tracklist.get_length().get() == 0:
            logger.debug("MusicWallFrontend: track_playback_ended- tracklist is empty - album has finished playing naturally, turning off peripheral lights")
            self.core.playback.stop()
            self.current_album = None
            peripheral_mac = self.mac_album_dict.get(self.current_album)
            self.send_to_esp(peripheral_mac, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])
        else:
            logger.debug(f"MusicWallFrontend: track_playback_ended - tracklist not empty, another track from `{self.current_album}` should be playing")


    def handle_command(self, cmd, new_message):
        try:
            # Build method name conventionally
            method_name = f"handle_{VALID_INCOMING_COMMANDS[int(cmd)]}"
            getattr(self, method_name)(new_message)
        except Exception as e:
            logger.warn(f"error: {e}")
            logger.warn(f"MusicWallFrontend - UNKNOWN COMMAND: {cmd}")
            logger.warn(f"MusicWallFrontend - method_name: '{method_name}' ")
            logger.warn(f"valid commands: {VALID_INCOMING_COMMANDS.keys()}")
            methods = [name for name in dir(self) if callable(getattr(self, name))]
            logger.warn(f"valid attributes: {methods}")
            valid_cmd = method_name in methods
            logger.warn(f"{method_name} a valid command? {valid_cmd}")
            logger.want(f"new_message: {new_message}")


    def handle_register(self, album):
        logger.debug(f"MusicWallFrontend: handle_command called with REGISTER: '{album}'")
        register_mac = self.mac_album_dict[album]
        self.send_to_esp(register_mac, REGISTER_ACK)


    def handle_debug(self, debug_message):
        logger.debug(f"MusicWallFrontend - DEBUG message from TRANSCEIVER: {debug_message}")


    def handle_stop(self, album):
        if self.current_album != album:
            logger.debug(f"MusicWallFrontend - handle_stop - album currently playing '{cur_album}' does not match stop command album '{album}'")
            return

        peripheral_mac_to_turn_off = self.mac_album_dict.get(self.current_album)
        self.current_album = None
        self.state = "stopped"
        self.core.playback.stop()
        self.core.tracklist.clear()
        self.send_to_esp(peripheral_mac_to_turn_off, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])


    def handle_play(self, new_album):
        logger.debug(f"MusicWallFrontend - handle_play - current_album: {self.current_album} - new_album: {new_album}")
        if new_album == self.current_album:
            logger.debug("MusicWallFrontend - handle_play - new_album is already playing, ignoring play command")
            return
        
        # 1. get tracks to add to tracklist
        album_uri = self.get_album_uri(new_album)
        track_uris = self.get_album_track_uris(album_uri)
        # need to do this before clearing tracklist
        # which will trigger tracklist_changed event
        
        # 2. stop the current album if one is playing
        if self.current_album:
            self.core.playback.stop()
            self.core.tracklist.clear()
            peripheral_mac_to_turn_off = self.mac_album_dict.get(self.current_album)
            self.send_to_esp(peripheral_mac_to_turn_off, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])

        # 3. play the new album
        self.current_album = new_album
        peripheral_mac_to_turn_on = self.mac_album_dict.get(self.current_album)
        logger.debug(f"MusicWallFrontend: playing track_uris {track_uris}")
        self.core.tracklist.add(uris=track_uris)
        self.core.playback.play()
        self.send_to_esp(peripheral_mac_to_turn_on, OUTGOING_SERIAL_COMMANDS["LIGHT_ON"])


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
        path = f"{media_dir}/{album_name}/"
        # Encode special characters (spaces, etc.) for a valid URI
        return f"file://{quote(path)}"


    def get_album_track_uris(self, album_uri: str):
        refs = self.core.library.browse(album_uri).get()
        track_uris = [ref.uri for ref in refs if ref.type == "track"]
        return track_uris


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
        track = self.core.playback.get_current_track().get()
        if track is None:
            logger.debug(f"MusicWallFrontend - get_current_album = nothing in tracklist")
            return None
        
        if track.uri:
            path = track.uri.replace("file://", "")
            folder = os.path.basename(os.path.dirname(path))
            logger.debug(f"MusicWallFrontend returning album name: '{folder}' from uri: {track.uri} - path: {path}")
            return folder

        logger.debug(f"MusicWallFrontend - missing track or track.uri - track: {track}")
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
            # logger.debug(f"MusicWallFrontend validated json: '{json.dumps(data)}'")
            mac = ":".join(f'{b:02X}' for b in data["mac"])
            message = data["message"]
            cmd = data["command"]
            
            if cmd != DEBUG:
                self.mac_album_dict[mac] = message
                self.mac_album_dict[message] = mac
            
            self.handle_command(cmd, message)
        else:
            logger.debug(f"MusicWallFrontend: JSON INVALID: '{line}' - len({len(line)})")
            if data in ["0", "1"]:
                self.handle_ack(data)
