import logging
import os
import threading
import time
import json
from mopidy_musicwall.types import SerialData, FrameRegistry
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
    "TOGGLE": TOGGLE,
    "LIGHT_ON": LIGHT_ON,
    "LIGHT_OFF": LIGHT_OFF,
    "NEW_CENTRAL": NEW_CENTRAL,
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
                logger.info(f"IncomingSerialHandler received: {line}")
                self.transform_serial(line)

    def transform_serial(self, raw_data):
        try:
            line = raw_data.decode('utf-8').rstrip().strip()
        except Exception as e:
            logger.info(f"MusicWallFrontend: DECODE ERROR FOR OBJ: '{raw_data}' - len({len(raw_data)})")
            logger.info(f"            ERROR: {e}")
            return None
        try:
            data = json.loads(line)
        except Exception as e:
            logger.info(f"MusicWallFrontend: JSON LOAD ERROR FOR OBJ: '{line}' - len({len(line)})")
            logger.info(f"            ERROR: {e}")
            return None
        try:
           serData = SerialData(**data)
        except Exception as e:
            logger.info(f"MusicWallFrontend: ERROR converting to SerialData for obj: '{data}' - len({len(data)})")
            logger.info(f"            ERROR: {e}")
            return None
        
        if serData.command == DEBUG:
            self.handle_debug(serData)
        else:
            self.frontend_proxy.handle_command(serData).get()

    def handle_debug(self, data: SerialData):
        logger.info(f"IncomingSerialHandler - DEBUG message from TRANSCEIVER: {data.message}")


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

        # Defaulting to a hidden file in the user's home or a specific mopidy path
        self.frame_registry = FrameRegistry()

        logger.info(f"MusicWallFrontend initialized on serial port {self.ser_port} with baudrate {self.baudrate}")
        logger.info(f"Loaded {len(self.frame_registry)} frames from DB.")


    ######## event handler callback functions ########
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
            self._on_track_playback_ended(kwargs.get("tl_track"))
        
        if event == "track_playback_started":
             self._on_track_playback_started(kwargs.get("tl_track"))
    

    ######## event callback handler helper functions ########
    def _on_track_playback_started(self, tl_track):
        logger.info(f"MusicWallFrontend: new track started from album: {tl_track.track.album.name}")
        frame = self.frame_registry.by_album_uri(tl_track.track.album.uri)
        if frame and not frame.is_lit:
            self._send_cmd_to_peripheral(frame.mac, LIGHT_ON)
            frame.is_lit = True


    def _on_track_playback_ended(self, tl_track):
        logger.info(f"MusicWallFrontend: track ended from album: {tl_track.track.album.name}")
        ending_album_frame = self.frame_registry.by_album_uri(tl_track.track.album.uri)
        if not ending_album_frame:
            # albums can be started by other services like iris so may not
            # always be associated with a frame
            return

        current_playing_album = self._get_currently_playing_album()

        is_manual_stop = not ending_album_frame.is_lit
        is_natural_end = not current_playing_album
        # ending album is associated with a frame and has been manually marked as turned off
        # OR track that just ended is the last track in the album, album has ended naturally
        if is_manual_stop or is_natural_end:
            self._send_cmd_to_peripheral(ending_album_frame.mac,  LIGHT_OFF)
            # redundent for manual_stops
            ending_album_frame.is_lit = False


    ######## HTTP handlers: they don't use SerialData ########
    def handle_toggle_http_request(self, peripheral_address):
        logger.info(f"MusicWallFrontend - received toggle request from http server for peripheral: {peripheral_address}")
        self._send_cmd_to_peripheral(peripheral_address, TOGGLE)


    def handle_new_album_association(self, frame_address, album_name):
        logger.info(f"new album association request for frame {frame_address} and album {album_name}")
        album_uri = self._get_album_uri(album_name)
        logger.info(f"new album uri: {album_uri}")
        self.frame_registry.add_or_update(frame_address, album_uri)
        updated = self.frame_registry.by_mac(frame_address)
        if updated:
            logger.info(f"frame registery updated: {updated.mac}: {updated.album_uri}")
        else:
            logger.warn(f"something went wrong! frame registry has no known mac {frame_address} after update")

    ######## Serial handlers: they do use SerialData ########
    def handle_command(self, data: SerialData):
        try:
            # Build method name conventionally
            method_name = f"handle_{VALID_INCOMING_COMMANDS[data.command]}"
            getattr(self, method_name)(data)
        except Exception as e:
            logger.warn(f"MusicWallFrontend handle_command error: {e}")


    def handle_register(self, data: SerialData):
        logger.info(f"MusicWallFrontend: _handle_register called from frame: '{data.mac_str}'")
        
        # if it's a new peripheral, add it's mac address to the db with an empty album
        # user needs to associate the frame with an album before we can play the album
        frame = self.frame_registry.by_mac(data.mac_str) 
        if not frame:
            logger.info(f"Registering new frame: {data.mac_str}")
            self.frame_registry.add_or_update(data.mac_str)

        # when a frame comes online, it sends a register command, even if it is already registered
        # if it doesn't receive an acknowledgement it will continue to try to register
        # need to acknowledge no matter what
        self._send_cmd_to_peripheral(data.mac, REGISTER_ACK)
        if frame and frame.is_lit:
            # a peripheral might go offline and then come back while
            # the record associated with it is already playing
            # in this case when it re-registers we need to turn it back on
            self._send_cmd_to_peripheral(data.mac, LIGHT_ON)


    def handle_info_response(self, data: SerialData):
        '''
        this should be deprecated
        '''
        logger.info(f"MusicWallFrontend - received info response from peripheral: {data.mac_str}")


    def handle_toggle(self, data: SerialData):
        '''Three states this handler handles:
            1. No album is playing, start the requested album
            2. An album is playing, and it's the requested album - stop it
            2. An album is playing, and it's a different album - stop the current one and start the requested one
        '''
        logger.info(f"MusicWallFrontend - handle_toggle request from {data.mac_str}")

        # step 1: validate data
        requesting_frame = self.frame_registry.by_mac(data.mac_str)
        if requesting_frame is None:
            logger.warn(f"MusicWallFrontend: unknown mac address requested {data.mac_str}")
            return
        if not requesting_frame.album_uri:
            logger.warn(f"MusicWallFrontend: no album has been associated with this frame {data.mac_str}")
            return
            # TODO: send message to peripheral to blink lights to indicate its not fully registered with an album

        current_playing_album = self._get_currently_playing_album()
        
        # nothing is playing, toggle requested album to ON
        if not current_playing_album:
            logger.info(f"MusicWallFrontend: no album currently playing.")
            self._play_new_album(requesting_frame)
            return
        
        # toggle current album to OFF
        if requesting_frame.album_uri == current_playing_album.uri:
            logger.info(f"MusicWallFrontend: requested album is current album. toggling album off")
            self._stop_current_album()
            return
        
        logger.info(f"current album is different from requested album: current_frame: {current_playing_album.uri} requested: {requesting_frame.album_uri}")
        # switch from one album to another
        current_playing_frame = self.frame_registry.by_album_uri(current_playing_album.uri)        
        # currently playing may not be associated with a frame if it was started by iris or some other frontend
        if current_playing_frame:
            # manually mark current playing frame as off so callback will turn off the light 
            current_playing_frame.is_lit = False
        self._stop_current_album()
        self._play_new_album(requesting_frame)
 

    def handle_skip(self, data: SerialData):
        '''skip the currently playing song'''
        current_album = self._get_currently_playing_album()
        if current_album and current_album.name:
            logger.info(f"skip requested from frame '{data.mac_str}'. Current Album: '{current_album.name}'")
            self.core.playback.next()


    ######## HELPER functions ########
    def _play_new_album(self, requesting_frame):
        logger.info(f"beginning to play new album: {requesting_frame.album_uri}")
        track_uris = self._get_album_track_uris(requesting_frame.album_uri)
        self.core.tracklist.add(uris=track_uris)
        self.core.playback.play().get()


    def _stop_current_album(self):
        self.core.playback.stop()
        self.core.tracklist.clear()


    def _get_currently_playing_album(self):
        state = self.core.playback.get_state().get()
        if state != "playing":
            logger.warning("MusicWallFrontend: Playback state is not 'playing'.")
            return None
        current_track = self.core.playback.get_current_track().get()
        logger.info(f"MusicWallFrontend: current playing album: {current_track.uri}")
        logger.info(f"MusicWallFrontend: current playing album: {current_track.name}")
        logger.info(f"MusicWallFrontend: current playing album: {current_track.album.uri}")
        return current_track.album if current_track else None
        

    def _get_album_uri(self, album_name: str, media_dir="/media/usb/music"):
        path = f"{media_dir}/{album_name}/"
        # Encode special characters (spaces, etc.) for a valid URI
        return f"file://{quote(path)}"


    def _get_album_track_uris(self, album_uri: str):
        refs = self.core.library.browse(album_uri).get()
        for ref in refs:
            logger.info(f"MusicWallFrontend: fileUri - {ref.uri} filetype - {ref.type}")
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
