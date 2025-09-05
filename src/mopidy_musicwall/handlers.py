from mopidy_musicwall.frontend import MusicWallFrontend
from tornado.escape import json_encode, json_decode
import tornado.web

import pykka
import logging

logger = logging.getLogger(__name__)


class HttpHandler(tornado.web.RequestHandler):
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
        
        if frontends and len(frontends) == 1:
            self.music_wall_frontend_proxy = frontends[0].proxy()
        elif (not frontends):
            logger.warn("MusicWallFrontend not found.")
        elif len(frontends) > 1:
            logger.warn("MusicWallFrontend not found or multiple instances found.")
        else:
            logger.warn("Soemthing weird happened.")
        
        self.music_wall_frontend_proxy = None

    # Options request
    # This is a preflight request for CORS requests
    # def options(self, slug=None):
    #     self.set_status(204)
    #     self.finish()


    def post(self):
        """Handle POST /play?mac=xx:xx:xx:xx:xx:xx"""
        mac = self.get_query_argument("mac", None)
        if not mac:
            self.set_status(400)
            self.write({"error": "Missing 'mac' parameter"})
            return

        logger.info("Received POST /play for MAC %s", mac)

        try:
            mac =  [int(b, 16) for b in mac.split(":")]
            self.music_wall_frontend_proxy.handle_toggle_http_request(mac).get()
        except Exception as e:
            logger.exception("Failed to handle play for %s", mac)
            self.set_status(500)
            self.write({"error": str(e)})
            return

        self.write({"status": "request successful", "smart frame mac address": mac})
