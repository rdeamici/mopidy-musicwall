import logging
import pathlib
from importlib.metadata import version

from mopidy import config, ext

__version__ = version("mopidy-musicwall")

# TODO: If you need to log, use loggers named after the current Python module
logger = logging.getLogger(__name__)


class Extension(ext.Extension):
    dist_name = "mopidy-musicwall"
    ext_name = "musicwall"
    version = __version__

    def get_default_config(self):
        return config.read(pathlib.Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        schema["port"] = config.String()
        schema["baudrate"] = config.Integer()
        return schema

    def setup(self, registry):
        # You will typically only implement one of the following things
        # in a single extension.

        # TODO: Edit or remove entirely
        from .frontend import MusicWallFrontend
        registry.add("frontend", MusicWallFrontend)

        # registry.add(
        #     "http:app", {"name": self.ext_name, "factory": musicwall_factory},
        # )

# def musicwall_factory(config, core):
#     from tornado.web import StaticFileHandler
#     from .handlers import HttpHandler

#     path = pathlib.Path(__file__).parent / "static"

#     return [
#         ('/play', HttpHandler, {"core": core, "config": config})
#     ]