import logging
import sys
from threading import Lock

LOCK = Lock()


def setup_logging(root: logging.Logger) -> None:
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    streamhandler = logging.StreamHandler(sys.stdout)
    streamhandler.setLevel(logging.INFO)
    streamhandler.setFormatter(formatter)

    root.addHandler(streamhandler)

    filehandler = logging.FileHandler("application.log")
    filehandler.setLevel(logging.DEBUG)
    filehandler.setFormatter(formatter)

    root.addHandler(filehandler)


def get_logger(name: str) -> logging.Logger:
    root = logging.getLogger("ambistream")

    if not root.hasHandlers():
        with LOCK:
            setup_logging(root)

    return root.getChild(name)
