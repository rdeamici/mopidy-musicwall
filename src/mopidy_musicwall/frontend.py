import logging
import os
import threading
import time
import json
from mopidy_musicwall.types import SerialData
import pykka
import requests
import io
import serial
from mopidy.core import CoreListener
from urllib.parse import quote
from enum import Enum, auto

from . import Extension

logger = logging.getLogger(__name__)

# current list of supported commands
REGISTER = 0
INFO_RESPONSE = 1
TOGGLE = 2
SKIP = 3
DEBUG = 4

VALID_INCOMING_COMMANDS = {
    REGISTER: "register",
    INFO_RESPONSE: "info_response",
    TOGGLE: "toggle",
    SKIP: "skip",
    DEBUG: "debug"
}
# ON = 1
# OFF = 2
# NEXT = 3
# PREVIOUS
# DEBUG = 4

REGISTER_ACK = 0
INFO_REQUEST = 1
TOGGLE = 2
LIGHT_ON = 3
LIGHT_OFF = 4
NEW_CENTRAL = 5

VALID_OUTGOING_COMMANDS = { REGISTER_ACK, INFO_REQUEST, TOGGLE, LIGHT_ON, LIGHT_OFF, NEW_CENTRAL }

OUTGOING_SERIAL_COMMANDS = {
    "REGISTER_ACK": REGISTER_ACK,
    "INFO_REQUEST": INFO_REQUEST,
    "LIGHT_ON": LIGHT_ON,
    "LIGHT_OFF": LIGHT_OFF,
    "NEW_CENTRAL": NEW_CENTRAL,
    "TOGGLE": TOGGLE
}

BROADCAST_ADDRESS = [255, 255, 255, 255, 255, 255]


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
                self.frontend_proxy.transform_serial(line).get()


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

        # reset system on central restart
        self._send_cmd_to_peripheral(BROADCAST_ADDRESS, OUTGOING_SERIAL_COMMANDS["NEW_CENTRAL"])
        self._send_cmd_to_peripheral(BROADCAST_ADDRESS, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])


    def on_stop(self):
        self.serial.close()
        self.incoming_handler.stop()
        self.outgoing_handler_proxy.actor_ref.stop()


    def on_event(self, event, **kwargs):
        # logger.info(f"MusicWallFrontend: on_event called with event: {event}, kwargs: {kwargs}")
        
        if event == "track_playback_ended":
            self.handle_track_playback_ended(kwargs.get("tl_track"))
        
        if event == "track_playback_started":
             self.handle_track_playback_started(kwargs.get("tl_track"))
    

    def handle_track_playback_started(self, tl_track):
        self.current_album = tl_track.track.album.name
        logger.info(f"MusicWallFrontend: new track started: current_album set to {self.current_album}")

    def handle_track_playback_ended(self, tl_track):
        '''Only used to handle the case where an album finishes naturally'''

        if self.current_album is None:
            logger.info("MusicWallFrontend: track_playback_ended - current_album is None, stop should have already been handled manually")
        
        elif self.current_album != tl_track.track.album.name:
            logger.info(f"MusicWallFrontend: track_playback_ended - old album `{tl_track.track.album.name}` ended, current album `{self.current_album}` should be playing")

        elif self.core.tracklist.get_length().get() == 0:
            self._handle_current_album_finished()
        else:
            logger.info(f"MusicWallFrontend: track_playback_ended - tracklist not empty, another track from `{self.current_album}` should be playing")


    def _handle_current_album_finished(self):
        logger.info("MusicWallFrontend: track_playback_ended- tracklist is empty - album has finished playing naturally, turning off peripheral lights")
        self.core.playback.stop()
        peripheral_mac: SerialData = self.mac_album_dict.get(self.current_album)
        if not peripheral_mac:
            logger.warn(f"current album finished! but we can't find the peripheral mac address :(")
            logger.warn(f"album dictionary: {self.mac_album_dict}")
        else:
            self._send_cmd_to_peripheral(peripheral_mac.mac_bytes , OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])
        
        self.current_album = None


    def _handle_command(self, data: SerialData):
        try:
            # Build method name conventionally
            method_name = f"handle_{VALID_INCOMING_COMMANDS[data.command]}"
            getattr(self, method_name)(data)
        except Exception as e:
            logger.warn(f"MusicWallFrontend _handle_command error: {e}")


    def handle_register(self, data: SerialData):
        logger.info(f"MusicWallFrontend: _handle_register called with: '{data.message}'")
        self._send_cmd_to_peripheral(data.mac_bytes, REGISTER_ACK)
        # a peripheral might go offline and then come back while
        # the record associated with it is already playing
        # in this case when it re-registers we need to turn it back on
        if self.current_album == data.message:
            self._send_cmd_to_peripheral(data.mac_bytes, OUTGOING_SERIAL_COMMANDS["LIGHT_ON"])


    def handle_debug(self, data: SerialData):
        logger.warn(f"MusicWallFrontend - DEBUG message from TRANSCEIVER: {data.message}")


    def handle_toggle_http_request(self, peripheral_address):
        logger.info(f"MusicWallFrontend - received toggle request from http server for peripheral: {peripheral_address}")
        self._send_cmd_to_peripheral(peripheral_address, OUTGOING_SERIAL_COMMANDS["TOGGLE"])


    def handle_info_response(self, data: SerialData):
        '''
        used to associate a peripheral mac address with an album.
        mac address and album is stored in dictionary in the
        transform serial step so nothing to do here.
        '''
        logger.info(f"MusicWallFrontend - received info response from peripheral: {data.message}")


    def handle_toggle(self, data: SerialData):
        '''Three states this handler handles:
            1. An album is playing, and it's the requested album - stop it
            2. An album is playing, and it's a different album - stop the current one and start the requested one
            3. No album is playing, start the requested album
        '''
        logger.info(f"MusicWallFrontend - handle_toggle - current_album: {self.current_album} - new_album: {data.message}")
        if data.message == self.current_album:
            logger.info(f"MusicWallFrontend - stop requested for album: {data.message}")
            peripheral_mac_to_turn_off: SerialData = self.mac_album_dict[self.current_album]
            self.current_album = None
            self.core.playback.stop()
            self.core.tracklist.clear()
            self._send_cmd_to_peripheral(peripheral_mac_to_turn_off.mac_bytes , OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])
            return
        
        if self.current_album:
            logger.info(f"stopping current album: {self.current_album}")
            self.core.playback.stop()
            self.core.tracklist.clear()
            peripheral_mac_to_turn_off: SerialData = self.mac_album_dict.get(self.current_album.lower())
            # may not exist if album was started via Iris for example
            if peripheral_mac_to_turn_off:
                self._send_cmd_to_peripheral(peripheral_mac_to_turn_off.mac_bytes, OUTGOING_SERIAL_COMMANDS["LIGHT_OFF"])

        self.current_album = data.message
        logger.info(f"beginning to play new album: {self.current_album}")
        album_uri = self._get_album_uri(self.current_album)
        track_uris = self._get_album_track_uris(album_uri)
        self.core.tracklist.add(uris=track_uris)
        self.core.playback.play().get()
        self._send_cmd_to_peripheral(data.mac_bytes, OUTGOING_SERIAL_COMMANDS["LIGHT_ON"])
        logger.info(f"playing new album: {self.current_album}")


    def handle_skip(self, data: SerialData):
        '''skip the currently playing song if the current album is the same as the peripheral that sent the request'''
        logger.info(f"skip requested for album '{data.message}'. Current Album: '{self.current_album}'")
        if data.message.lower() == self.current_album.lower():
            self.core.playback.next()


    def _get_album_uri(self, album_name: str, media_dir="/media/usb/music"):
        # Replace spaces with underscores to match your filesystem
        path = f"{media_dir}/{album_name}/"
        # Encode special characters (spaces, etc.) for a valid URI
        return f"file://{quote(path)}"


    def _get_album_track_uris(self, album_uri: str):
        refs = self.core.library.browse(album_uri).get()
        track_uris = [ref.uri for ref in refs if ref.type == "track"]
        return track_uris


    def _send_cmd_to_peripheral(self, mac: list[int], command: int):
        logger.info(f"in send_cmd_to_peripheral: mac - {mac} - command - {command}")
        payload = {
            "mac": mac,
            "command": command
        }
        json_str = json.dumps(payload)
        self.outgoing_handler_proxy.send_message(json_str)


    def transform_serial(self, data):
        try:
            line = data.decode('utf-8').rstrip().strip()
            data = json.loads(line)
            serData = SerialData(**data)
        except Exception as e:
            logger.info(f"MusicWallFrontend: DECODE ERROR FOR OBJ: '{line}' - len({len(line)})")
            logger.info(f"            ERROR: {e}")
            return None

        message = data["message"]
        cmd = data["command"]
            
        # debug messages come from central - no need to register central
        if cmd != DEBUG:
            self.mac_album_dict[serData.mac_str] = message
            self.mac_album_dict[message] = serData
        
        self._handle_command(serData)
