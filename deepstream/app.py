import sys
import os
import logging
from pipeline import main as pipeline_main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("VisionApp")

def main():
    logger.info("Iniciando Vision Stability Lab via app.py...")
    pipeline_main()

if __name__ == "__main__":
    main()
