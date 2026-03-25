from mopidy_musicwall.frontend import MusicWallFrontend
from tornado.escape import json_encode, json_decode
import tornado.web

import pykka
import logging

logger = logging.getLogger(__name__)


class BaseMusicWallHttpHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header(
            "Access-Control-Allow-Headers",
            (
                "Origin, X-Requested-With, Content-Type, Accept, "
                "Authorization, Client-Security-Token, Accept-Encoding"
            ),
        )

    def initialize(self, core, config):
        self.core = core
        self.config = config
        frontends = pykka.ActorRegistry.get_by_class(MusicWallFrontend)
        logger.info("MusicWallHttpHandler: initializing...")
        if frontends and len(frontends) == 1:
            self.music_wall_frontend_proxy = frontends[0].proxy()
        elif (not frontends):
            logger.warn("MusicWallFrontend not found.")
            self.music_wall_frontend_proxy = None
        elif len(frontends) > 1:
            logger.warn("MusicWallFrontend not found or multiple instances found.")
            self.music_wall_frontend_proxy = None
        else:
            logger.warn("Soemthing weird happened.")
            self.music_wall_frontend_proxy = None



class PlayHandler(BaseMusicWallHttpHandler):
    def get(self):
        """Handle POST /play?mac=xx:xx:xx:xx:xx:xx"""
        mac_str = self.get_argument("mac", None)
        if not mac_str:
            self.set_status(400)
            self.write({"error": "Missing 'mac' parameter"})
            return

        logger.info("Received POST /play for MAC %s", mac_str)

        try:
            mac_list =  [int(b, 16) for b in mac_str.split(":")]
            self.music_wall_frontend_proxy.handle_toggle_http_request(mac_list).get()
        except Exception as e:
            logger.exception("Failed to handle play for %s", mac_str)
            self.set_status(500)
            self.write({"error": str(e)})
            return

        self.write({"status": "request successful", "smart frame mac address": mac_str})



class UpdateHandler(BaseMusicWallHttpHandler):
    # when typing urls into a browser, the browser can only send get requests
    # TODO: create a proper frontend UI for this action

    def get(self):
        """Handle GET /update?mac=xx:xx:xx:xx:xx:xx&album_name=albumName"""
        mac_str = self.get_argument("mac", None)
        album_name = self.get_argument("album_name", None)

        if not mac_str or not album_name:
            self.set_status(400)
            self.write({"error": "Missing required parameters"})
            return

        logger.info("Received get /update for MAC %s with Album %s", mac_str, album_name)

        try:
            # We pass the strings to the frontend actor. 
            # The Actor will handle calling registry.add_or_update(mac_str, album_uri)
            self.music_wall_frontend_proxy.handle_new_album_association(mac_str, album_name).get()
            
            self.write({
                "status": "association updated",
                "mac": mac_str,
                "album_name": album_name
            })
        except Exception as e:
            logger.exception("Failed to update album for %s", mac_str)
            self.set_status(500)
            self.write({"error": str(e)})