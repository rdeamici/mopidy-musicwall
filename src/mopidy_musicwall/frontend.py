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
    "INFO": INFO,
    "LIGHT_ON": LIGHT_ON,
    "LIGHT_OFF": LIGHT_OFF,
    "POWER_ON": POWER_ON,
    "POWER_OFF": POWER_OFF,
    "NEW_CENTRAL": NEW_CENTRAL
}

BROADCAST_ADDRESS = "0xFF:0xFF:0xFF:0xFF:0xFF:0xFF"


class OutgoingSerialHandler(pykka.ThreadingActor):
    def __init__(self, serial_port):
        super(OutgoingSerialHandler, self).__init__()
        self.serial_port = serial_port
        self.lock = threading.Lock()
        logger.info("OutgoingSerialHandler initialized")
        # logger.info(f"OutgoingSerialHandler serial_port is open? {self.serial_port.is_open}")


    def send_message(self, message: str):
        with self.lock:
            logger.info(f"OutgoingSerialHandler sending message: {message}")
            try:
                numBytes = self.serial_port.write((message + '\n').encode('utf-8'))   # newline is important
            except Exception as e:
                logger.info(f"OutgoingSerialHandler encountered an error: {e}")


class IncomingSerialHandler(pykka.ThreadingActor):
    def __init__(self, serial_port, frontend_proxy):
        super(IncomingSerialHandler, self).__init__()
        self.frontend_proxy = frontend_proxy
        self.serial_port = serial_port
        logger.info("IncomingSerialHandler initialized")
        # logger.info(f"IncomingSerialHandler serial_port is open? {self.serial_port.is_open}")


    def on_start(self):
        self.running = True
        self.thread = threading.Thread(target=self.read_loop, daemon=True)
        self.thread.start()    


    def read_loop(self):
        while self.running:
            line = self.serial_port.readline()
            if line:
                # logger.info(f"IncomingSerialHandler received: {line}")
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
        logger.info(f"MusicWallFrontend initialized on serial port {self.ser_port} with baudrate {self.baudrate}")


    def on_start(self):
        self.serial = serial.Serial(self.ser_port, self.baudrate, timeout=1)
        self.incoming_handler = IncomingSerialHandler.start(self.serial, self.actor_ref.proxy())
        self.outgoing_handler_proxy = OutgoingSerialHandler.start(self.serial).proxy()
        self.core.tracklist.set_consume(True)

        # send out a new central command to any peripherals that may be on while central restarted
        # will cause the peripherals to reset themselves
        self.send_cmd_to_peripheral(BROADCAST_ADDRESS, OUTGOING_SERIAL_COMMANDS["NEW_CENTRAL"])


    def on_stop(self):
        self.serial.close()
        self.incoming_handler.stop()
        self.outgoing_handler_proxy.actor_ref.stop()


    def on_event(self, event, **kwargs):
        # logger.info(f"MusicWallFrontend: on_event called with event: {event}, kwargs: {kwargs}")
        
        if event == "track_playback_ended":
            self.handle_track_playback_ended(kwargs.get("tl_track"))    
    

    def handle_track_playback_ended(self, tl_track):
        '''Only used to handle the case where an album finishes naturally'''

        if self.current_album is None:
            logger.info("MusicWallFrontend: track_playback_ended - current_album is None, stop should have already been handled manually")
        
        elif self.current_album != tl_track.track.album:
            logger.info(f"MusicWallFrontend: track_playback_ended - old album `{tl_track.track.album}` ended, current album `{self.current_album}` should be playing")

        elif self.core.tracklist.get_length().get() == 0:
            self._handle_current_album_finished()
        else:
            logger.info(f"MusicWallFrontend: track_playback_ended - tracklist not empty, another track from `{self.current_album}` should be playing")


    def _handle_current_album_finished(self):
        logger.info("MusicWallFrontend: track_playback_ended- tracklist is empty - album has finished playing naturally, turning off peripheral lights")
        self.core.playback.stop()
        peripheral_mac = self.mac_album_dict[self.current_album]
        self.send_cmd_to_peripheral(peripheral_mac, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])
        self.current_album = None


    def handle_command(self, cmd, message):
        try:
            # Build method name conventionally
            method_name = f"handle_{VALID_INCOMING_COMMANDS[int(cmd)]}"
            getattr(self, method_name)(message)
        except Exception as e:
            logger.warn(f"MusicWallFrontend handle_command error: {e}")


    def handle_register(self, album):
        logger.info(f"MusicWallFrontend: handle_command called with REGISTER: '{album}'")
        register_mac = self.mac_album_dict[album]
        self.send_cmd_to_peripheral(register_mac, REGISTER_ACK)
        # a peripheral might go offline and then come back while
        # the record associated with it is already playing
        if self.current_album == album:
            self.send_cmd_to_peripheral(register_mac, OUTGOING_SERIAL_COMMANDS["LIGHT_ON"])


    def handle_debug(self, debug_message):
        logger.warn(f"MusicWallFrontend - DEBUG message from TRANSCEIVER: {debug_message}")


    def handle_stop(self, album):
        if self.current_album != album:
            logger.info(f"MusicWallFrontend - ERROR - tried to stop album '{album}' but which is not playing: album '{self.current_album}' is - ignoring stop command")
            return

        logger.info(f"MusicWallFrontend - stop requested for album: {album}")
        peripheral_mac_to_turn_off = self.mac_album_dict.get(self.current_album)
        self.current_album = None
        self.core.playback.stop()
        self.core.tracklist.clear()
        self.send_cmd_to_peripheral(peripheral_mac_to_turn_off, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])


    def handle_play(self, new_album):
        logger.info(f"MusicWallFrontend - handle_play - current_album: {self.current_album} - new_album: {new_album}")
        if new_album == self.current_album:
            logger.info("MusicWallFrontend - handle_play - new_album is already playing, ignoring play command")
            return
        
        # 1. get tracks to add to tracklist
        album_uri = self.get_album_uri(new_album)
        track_uris = self.get_album_track_uris(album_uri)
        
        # 2. stop the current album if one is playing
        if self.current_album:
            logger.info(f"stopping current album: {self.current_album}")
            self.core.playback.stop()
            self.core.tracklist.clear()
            peripheral_to_turn_off = self.mac_album_dict[self.current_album]
            self.send_cmd_to_peripheral(peripheral_to_turn_off, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])

        # 3. play the new album
        self.current_album = new_album
        peripheral_to_turn_on = self.mac_album_dict[self.current_album]
        # logger.info(f"MusicWallFrontend: playing track_uris {track_uris}")
        self.core.tracklist.add(uris=track_uris)
        logger.info(f"playing new album: {self.current_album}")
        self.core.playback.play()
        self.send_cmd_to_peripheral(peripheral_to_turn_on, OUTGOING_SERIAL_COMMANDS["LIGHT_ON"])


    def get_album_uri(self, album_name: str, media_dir="/media/usb/music"):
        # Replace spaces with underscores to match your filesystem
        path = f"{media_dir}/{album_name}/"
        # Encode special characters (spaces, etc.) for a valid URI
        return f"file://{quote(path)}"


    def get_album_track_uris(self, album_uri: str):
        refs = self.core.library.browse(album_uri).get()
        track_uris = [ref.uri for ref in refs if ref.type == "track"]
        return track_uris


    def send_cmd_to_peripheral(self, mac_address, command):
        mac = [int(b, 16) for b in mac_address.split(":")]
        payload = {
            "mac": mac,
            "command": command
        }
        json_str = json.dumps(payload)
        self.outgoing_handler_proxy.send_message(json_str)


    def get_current_album(self):
        track = self.core.playback.get_current_track().get()
        if track is None:
            logger.info(f"MusicWallFrontend - get_current_album = nothing in tracklist")
            return None
        
        if track.uri:
            path = track.uri.replace("file://", "")
            folder = os.path.basename(os.path.dirname(path))
            return folder

        logger.info(f"MusicWallFrontend - missing track or track.uri - track: {track}")
        return None


    def transform_serial(self, data):
        line = data.decode('utf-8').rstrip().strip()
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.info(f"MusicWallFrontend: JSON DECODE ERROR FOR OBJ: '{line}' - len({len(line)})")
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
            # logger.info(f"MusicWallFrontend validated json: '{json.dumps(data)}'")
            mac = ":".join(f'{b:02X}' for b in data["mac"])
            message = data["message"]
            cmd = data["command"]
            
            # debug messages come from central - no need to register central
            if cmd != DEBUG:
                self.mac_album_dict[mac] = message
                self.mac_album_dict[message] = mac
            
            self.handle_command(cmd, message)
        else:
            logger.warn(f"MusicWallFrontend: JSON INVALID: '{line}' - len({len(line)})")
