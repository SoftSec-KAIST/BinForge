import logging
import coloredlogs

coloredlogs.install(level=logging.INFO, fmt="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger()

__all__ = ["logger"]

# EOF
